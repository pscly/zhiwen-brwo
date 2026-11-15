# zw-hub · 知纹同步中心

局域网内托管**加密** profile 快照、协调跨机租约的小服务。

## 它不做什么

- 不解密任何东西（只存 AEAD 密文）
- 不需要数据库（一个目录就是全部状态）
- 不需要对象存储（可换成 S3，但默认用本地卷）

## 跑起来

```bash
cp .env.example .env       # 填一个足够长的 ZW_HUB_TOKEN
docker compose up -d
curl -s http://127.0.0.1:8700/healthz
```

## 接口

见主仓库 `docs/04-接口文档.md`，或者直接读给 AI 的自描述：

```bash
curl -s -H "Authorization: Bearer $ZW_HUB_TOKEN" http://127.0.0.1:8700/v1/ai/manifest | jq
```

## 目录结构（容器内 `/data`）

```
blobs/<环境id>/v<N>.blob        加密快照
blobs/<环境id>/v<N>.meta.json   元信息（明文，便于排错）
latest/<环境id>.json            最新版本指针
leases/<环境id>.json            跨机租约
logs/hub.log
```
