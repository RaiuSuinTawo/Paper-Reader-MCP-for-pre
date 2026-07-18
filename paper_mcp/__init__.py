"""Paper-Assistant-MCP —— 基于 MinerU 云端 API 重构的论文阅读 MCP Server。

分层:
- config    : 配置与路径解析
- mineru     : MinerU 云端 API 客户端(纯 HTTP)
- cache      : 本地缓存与 manifest
- content    : 检索/阅读层(在 content_list.json 之上)
- parsing    : 解析编排(提交 / 轮询 / 便捷等待)
- output     : 产出层(写文件 / 收集图片)
- server     : FastMCP 实例与工具/资源/提示注册
"""

__all__ = ["server"]
