"""
Budget-pacing controller, driven by real (streamed) auction and click data
instead of an in-process simulation loop.

A Kafka producer (src/kafka_producer.py) publishes every auction outcome to
`bc-auctions` and every realized click to `bc-clicks`. This job consumes
both with Spark Structured Streaming, aggregates spend/wins/clicks into
1-minute tumbling windows per campaign, and on every micro-batch:

  1. updates the campaign's cumulative spend,
  2. re-runs the same PI-style pacing controller used previously
     (`update_pacing_multiplier`) against the target spend curve, and
  3. publishes the new multiplier to `bc-pacing-mult`, which the producer
     reads back to scale its next bids.

This keeps the pacing math from the original in-process simulator, but the
stats it reacts to now come from Kafka instead of a synchronous auction
loop, and the aggregation itself is done by Spark rather than by hand.

Requires: pyspark
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import BooleanType, DoubleType, StringType, StructType

from src.models import clamp

# -----------------------------
# Pacing controller (unchanged control logic)
# -----------------------------

@dataclass
class PacingConfig:
    # pacing multiplier bounds
    mult_min: float = 0.1
    mult_max: float = 5.0

    # update rule parameters (simple PI-like)
    kp: float = 0.5
    ki: float = 0.05

    # anti-windup clamp on integral term
    integ_min: float = -5.0
    integ_max: float = 5.0

    # smoothing on multiplier updates
    smooth: float = 0.2  # 0..1, higher = faster changes

@dataclass
class PacingState:
    multiplier: float = 1.0
    integ_err: float = 0.0
    start_time: Optional[datetime] = None  # first window seen for this campaign
    cum_spend: float = 0.0
    cum_wins: int = 0
    cum_auctions: int = 0
    cum_clicks: int = 0

def update_pacing_multiplier(
    cfg: PacingConfig,
    st: PacingState,
    target_spend_so_far: float,
    actual_spend_so_far: float,
) -> float:
    """
    Control objective: track cumulative spend curve.
    error > 0 => behind => increase multiplier.
    """
    # normalize error to avoid scaling headaches
    denom = max(1e-6, target_spend_so_far)
    err = (target_spend_so_far - actual_spend_so_far) / denom  # + means behind

    st.integ_err = clamp(st.integ_err + err, cfg.integ_min, cfg.integ_max)

    raw = 1.0 + cfg.kp * err + cfg.ki * st.integ_err
    raw = clamp(raw, cfg.mult_min, cfg.mult_max)

    # smooth multiplicative changes (EMA in multiplier space)
    st.multiplier = (1.0 - cfg.smooth) * st.multiplier + cfg.smooth * raw
    st.multiplier = clamp(st.multiplier, cfg.mult_min, cfg.mult_max)
    return st.multiplier

def linear_spend_curve(total_budget: float, t_minutes: float, horizon_minutes: float) -> float:
    """
    Target cumulative spend by elapsed minute t (0..horizon_minutes).
    Swap with: intraday curve, learned curve, front-load/back-load.
    """
    frac = clamp(t_minutes / max(1.0, horizon_minutes), 0.0, 1.0)
    return total_budget * frac

# -----------------------------
# Streaming job config
# -----------------------------

@dataclass
class StreamingPacingConfig:
    kafka_bootstrap: str = "localhost:9092"
    auctions_topic: str = "bc-auctions"
    clicks_topic: str = "bc-clicks"
    pacing_mult_topic: str = "bc-pacing-mult"
    checkpoint_dir: str = "/tmp/checkpoints/bc_bidding_pacing"

    window_duration: str = "1 minute"
    watermark_delay: str = "2 minutes"
    trigger_interval: str = "30 seconds"

    # budget plan the controller paces against
    total_budget: float = 100.0
    horizon_minutes: float = 6 * 60

AUCTIONS_SCHEMA = (
    StructType()
    .add("auction_id", StringType())
    .add("campaign_id", StringType())
    .add("event_time_epoch", DoubleType())
    .add("bid", DoubleType())
    .add("pctr", DoubleType())
    .add("won", BooleanType())
    .add("price", DoubleType())
)

CLICKS_SCHEMA = (
    StructType()
    .add("auction_id", StringType())
    .add("campaign_id", StringType())
    .add("event_time_epoch", DoubleType())
)


def _read_json_stream(spark: SparkSession, cfg: StreamingPacingConfig, topic: str, schema: StructType) -> DataFrame:
    raw = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", cfg.kafka_bootstrap)
        .option("subscribe", topic)
        .option("startingOffsets", "latest")
        .load()
    )
    parsed = raw.select(F.from_json(F.col("value").cast("string"), schema).alias("data")).select("data.*")
    return parsed.withColumn("event_time", F.expr("timestamp_seconds(event_time_epoch)")).drop("event_time_epoch")


def _elapsed_minutes(state: PacingState, window_end: datetime) -> float:
    if state.start_time is None:
        state.start_time = window_end
    return max(0.0, (window_end - state.start_time).total_seconds() / 60.0)


def make_auctions_batch_handler(
    pacing_cfg: PacingConfig,
    sim_cfg: StreamingPacingConfig,
    campaign_states: Dict[str, PacingState],
):
    def handle(batch_df: DataFrame, batch_id: int) -> None:
        rows = batch_df.orderBy("window").collect()
        if not rows:
            return

        out_rows = []
        for r in rows:
            campaign_id = r["campaign_id"]
            state = campaign_states.setdefault(campaign_id, PacingState())

            state.cum_spend += r["minute_spend"] or 0.0
            state.cum_wins += r["minute_wins"] or 0
            state.cum_auctions += r["minute_auctions"] or 0

            elapsed = _elapsed_minutes(state, r["window"]["end"])
            target_cum = linear_spend_curve(sim_cfg.total_budget, elapsed, sim_cfg.horizon_minutes)
            mult = update_pacing_multiplier(pacing_cfg, state, target_cum, state.cum_spend)

            print(
                f"[auctions] campaign={campaign_id} window_end={r['window']['end']} "
                f"spend={state.cum_spend:.2f}/{target_cum:.2f} mult={mult:.3f} "
                f"wins={state.cum_wins} auctions={state.cum_auctions} "
                f"win_rate={state.cum_wins / max(1, state.cum_auctions):.3f}",
                flush=True,
            )

            out_rows.append((campaign_id, mult, state.cum_spend, target_cum))

        out_df = batch_df.sparkSession.createDataFrame(
            out_rows, ["campaign_id", "pacing_mult", "cum_spend", "target_cum_spend"]
        )
        (
            out_df.select(
                F.col("campaign_id").alias("key"),
                F.to_json(F.struct("campaign_id", "pacing_mult", "cum_spend", "target_cum_spend")).alias("value"),
            )
            .write.format("kafka")
            .option("kafka.bootstrap.servers", sim_cfg.kafka_bootstrap)
            .option("topic", sim_cfg.pacing_mult_topic)
            .save()
        )

    return handle


def make_clicks_batch_handler(campaign_states: Dict[str, PacingState]):
    def handle(batch_df: DataFrame, batch_id: int) -> None:
        rows = batch_df.orderBy("window").collect()
        for r in rows:
            campaign_id = r["campaign_id"]
            state = campaign_states.setdefault(campaign_id, PacingState())
            state.cum_clicks += r["minute_clicks"] or 0
            ctr = state.cum_clicks / max(1, state.cum_wins)
            print(
                f"[clicks]   campaign={campaign_id} window_end={r['window']['end']} "
                f"clicks={state.cum_clicks} ctr_on_wins={ctr:.4f}",
                flush=True,
            )

    return handle


def run_pacing_controller(sim_cfg: StreamingPacingConfig, pacing_cfg: PacingConfig) -> None:
    import pyspark

    scala_version = "2.13" if pyspark.__version__ >= "4" else "2.12"
    kafka_package = f"org.apache.spark:spark-sql-kafka-0-10_{scala_version}:{pyspark.__version__}"

    spark = (
        SparkSession.builder.appName("BudgetPacingController")
        .config("spark.jars.packages", kafka_package)
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    auctions = _read_json_stream(spark, sim_cfg, sim_cfg.auctions_topic, AUCTIONS_SCHEMA)
    clicks = _read_json_stream(spark, sim_cfg, sim_cfg.clicks_topic, CLICKS_SCHEMA)

    auctions_agg = (
        auctions.withWatermark("event_time", sim_cfg.watermark_delay)
        .groupBy(F.window("event_time", sim_cfg.window_duration), "campaign_id")
        .agg(
            F.sum(F.when(F.col("won"), F.col("price")).otherwise(0.0)).alias("minute_spend"),
            F.sum(F.col("won").cast("int")).alias("minute_wins"),
            F.count("*").alias("minute_auctions"),
        )
    )

    clicks_agg = (
        clicks.withWatermark("event_time", sim_cfg.watermark_delay)
        .groupBy(F.window("event_time", sim_cfg.window_duration), "campaign_id")
        .agg(F.count("*").alias("minute_clicks"))
    )

    # shared across both queries (foreachBatch runs on the driver, so a plain
    # dict is fine for this single-instance demo; a real deployment would
    # keep this in an external store so the controller can be replicated).
    campaign_states: Dict[str, PacingState] = {}

    auctions_query = (
        auctions_agg.writeStream.outputMode("update")
        .foreachBatch(make_auctions_batch_handler(pacing_cfg, sim_cfg, campaign_states))
        .option("checkpointLocation", f"{sim_cfg.checkpoint_dir}/auctions")
        .trigger(processingTime=sim_cfg.trigger_interval)
        .start()
    )

    clicks_query = (
        clicks_agg.writeStream.outputMode("update")
        .foreachBatch(make_clicks_batch_handler(campaign_states))
        .option("checkpointLocation", f"{sim_cfg.checkpoint_dir}/clicks")
        .trigger(processingTime=sim_cfg.trigger_interval)
        .start()
    )

    spark.streams.awaitAnyTermination()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spark Structured Streaming budget-pacing controller")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument("--auctions-topic", default="bc-auctions")
    parser.add_argument("--clicks-topic", default="bc-clicks")
    parser.add_argument("--pacing-mult-topic", default="bc-pacing-mult")
    parser.add_argument("--checkpoint-dir", default="/tmp/checkpoints/bc_bidding_pacing")
    parser.add_argument("--total-budget", type=float, default=100.0)
    parser.add_argument("--horizon-minutes", type=float, default=6 * 60)
    args = parser.parse_args()

    sim_cfg = StreamingPacingConfig(
        kafka_bootstrap=args.bootstrap_servers,
        auctions_topic=args.auctions_topic,
        clicks_topic=args.clicks_topic,
        pacing_mult_topic=args.pacing_mult_topic,
        checkpoint_dir=args.checkpoint_dir,
        total_budget=args.total_budget,
        horizon_minutes=args.horizon_minutes,
    )
    pacing_cfg = PacingConfig(kp=0.8, ki=0.05, smooth=0.3, mult_min=0.2, mult_max=3.0)

    run_pacing_controller(sim_cfg, pacing_cfg)
