from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StringType, DoubleType, TimestampType

spark = SparkSession.builder \
    .appName("AdBudgetControl") \
    .getOrCreate()

# 假设日志从 Kafka 流入，每条消息是一次广告点击/曝光事件
# JSON 格式: {"campaign_id": "C001", "event_type": "click", "cost": 1.5, "event_time": "2026-07-20T10:00:00"}

schema = StructType() \
    .add("campaign_id", StringType()) \
    .add("event_type", StringType()) \
    .add("cost", DoubleType()) \
    .add("event_time", TimestampType())

raw_stream = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "localhost:9092") \
    .option("subscribe", "ad_events") \
    .load()

# 解析 Kafka 的 value 字段（原始是 JSON 字符串）
events = raw_stream.select(
    F.from_json(F.col("value").cast("string"), schema).alias("data")
).select("data.*")

# 处理乱序数据：允许 2 分钟的延迟
events_with_watermark = events.withWatermark("event_time", "2 minutes")

# 按 campaign_id + 5分钟滚动窗口 统计实时花费
spend_per_window = events_with_watermark \
    .groupBy(
        F.window("event_time", "5 minutes"),
        "campaign_id"
    ) \
    .agg(F.sum("cost").alias("total_spend"))

# 关联预算表（每个campaign的预算上限），标记超预算的campaign
budget_df = spark.read.csv("campaign_budgets.csv", header=True, inferSchema=True)
# 列: campaign_id, daily_budget

alerts = spend_per_window.join(F.broadcast(budget_df), "campaign_id") \
    .withColumn("over_budget", F.col("total_spend") >= F.col("daily_budget")) \
    .filter(F.col("over_budget") == True) \
    .select("campaign_id", "window", "total_spend", "daily_budget")

# 输出：写入 Kafka 的 "budget_alerts" topic，下游服务订阅后自动暂停投放
query = alerts.select(
        F.col("campaign_id").cast("string").alias("key"),
        F.to_json(F.struct("campaign_id", "total_spend", "daily_budget")).alias("value")
    ) \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "localhost:9092") \
    .option("topic", "budget_alerts") \
    .option("checkpointLocation", "/tmp/checkpoints/budget_control") \
    .outputMode("update") \
    .trigger(processingTime="30 seconds") \
    .start()

query.awaitTermination()