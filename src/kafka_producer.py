"""
Simulated bidder: generates auctions in (near) real time, applies the
current pacing multiplier, and publishes the outcome to Kafka.

Two topics are produced, mirroring how a real ad server separates its
impression/auction log from its click log:

  - `bc-auctions`: one record per auction (bid, win decision, clearing
    price if won, pCTR).
  - `bc-clicks`: one record per realized click, referencing the auction
    that produced it.

The pacing multiplier itself is *not* computed here -- it is read from
`bc-pacing-mult`, which src/bc_bidding_pacing.py (the Spark Structured
Streaming job) keeps updated from the actual spend it observes on
`bc-auctions`. This closes the loop: producer -> Kafka -> Spark -> Kafka -> producer.

Requires: kafka-python (`pip install kafka-python`)
"""

import argparse
import json
import random
import threading
import time
import uuid
from typing import Optional

from kafka import KafkaConsumer, KafkaProducer

from src.models import (
    base_bid_from_value,
    clamp,
    clearing_price,
    pctr_model,
    sample_auction,
    value_per_click,
    win_prob,
)


class LivePacingMultiplier:
    """Background-updated pacing multiplier, fed by the Spark job's output topic."""

    def __init__(self, bootstrap_servers: str, topic: str, campaign_id: str):
        self._campaign_id = campaign_id
        self._value = 1.0
        self._lock = threading.Lock()
        self._consumer = KafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            auto_offset_reset="latest",
            enable_auto_commit=True,
            group_id=f"bc-producer-{campaign_id}-{uuid.uuid4().hex[:8]}",
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for msg in self._consumer:
            data = msg.value
            if data.get("campaign_id") != self._campaign_id:
                continue
            with self._lock:
                self._value = float(data["pacing_mult"])

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


def run_producer(
    bootstrap_servers: str,
    campaign_id: str,
    auctions_topic: str,
    clicks_topic: str,
    pacing_mult_topic: str,
    auctions_per_minute: float,
    bid_min: float,
    bid_max: float,
    seed: Optional[int],
) -> None:
    if seed is not None:
        random.seed(seed)

    producer = KafkaProducer(
        bootstrap_servers=bootstrap_servers,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8"),
    )

    live_mult = LivePacingMultiplier(bootstrap_servers, pacing_mult_topic, campaign_id)

    sleep_s = 60.0 / max(1e-6, auctions_per_minute)
    print(
        f"[producer] campaign={campaign_id} rate={auctions_per_minute}/min "
        f"(~{sleep_s:.3f}s/auction) -> {auctions_topic} / {clicks_topic}"
    )

    try:
        while True:
            auction = sample_auction()
            pctr = pctr_model(auction)
            base_bid = base_bid_from_value(pctr, value_per_click())
            bid = clamp(base_bid * live_mult.value, bid_min, bid_max)

            auction_id = uuid.uuid4().hex
            now = time.time()
            won = random.random() < win_prob(bid, auction)
            price = clearing_price(bid, auction) if won else None

            producer.send(
                auctions_topic,
                key=campaign_id,
                value={
                    "auction_id": auction_id,
                    "campaign_id": campaign_id,
                    "event_time_epoch": now,
                    "bid": bid,
                    "pctr": pctr,
                    "won": won,
                    "price": price,
                },
            )

            if won and random.random() < pctr:
                producer.send(
                    clicks_topic,
                    key=campaign_id,
                    value={
                        "auction_id": auction_id,
                        "campaign_id": campaign_id,
                        "event_time_epoch": time.time(),
                    },
                )

            time.sleep(sleep_s)
    except KeyboardInterrupt:
        pass
    finally:
        producer.flush()
        producer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulated bidder publishing auctions/clicks to Kafka")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument("--campaign-id", default="sim-campaign-1")
    parser.add_argument("--auctions-topic", default="bc-auctions")
    parser.add_argument("--clicks-topic", default="bc-clicks")
    parser.add_argument("--pacing-mult-topic", default="bc-pacing-mult")
    parser.add_argument("--auctions-per-minute", type=float, default=200.0)
    parser.add_argument("--bid-min", type=float, default=0.0)
    parser.add_argument("--bid-max", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    run_producer(
        bootstrap_servers=args.bootstrap_servers,
        campaign_id=args.campaign_id,
        auctions_topic=args.auctions_topic,
        clicks_topic=args.clicks_topic,
        pacing_mult_topic=args.pacing_mult_topic,
        auctions_per_minute=args.auctions_per_minute,
        bid_min=args.bid_min,
        bid_max=args.bid_max,
        seed=args.seed,
    )
