# 主题：Kafka 高可用

> 种子题库（格式样板）。解析契约见 dataflows/local_corpus.py：`---` 分块，
> 块头 key: value（ASCII 冒号），块身按 **小节名** 切分。本行以上的文件头解析器跳过。

---
id: bank-kafka-001
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列, 高可用
source: 自编·对照《Kafka 权威指南》第3章

**题目**
三节点 Kafka 集群里，leader 副本所在机器突然宕机：集群如何保证已 ack 的消息不丢？说说 ISR、acks、min.insync.replicas 各自在其中的作用。

**追问树**
- acks=all 和 acks=1 的取舍是什么？各自适用什么场景？
- min.insync.replicas 设成 1 有什么风险？
- unclean.leader.election 打开会发生什么？

**评分锚点**
- 5 分：说清 ISR/acks/min.insync.replicas 三者联动，能指出丢消息的精确窗口
- 3 分：知道主从切换机制，但说不清什么配置组合下会丢消息
- 1 分：只答"有副本所以不丢"
