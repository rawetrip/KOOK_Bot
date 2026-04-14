# KOOK Bot
基于 KOOK 平台和 Python 开发的企业级高并发多功能游戏机器人。
本项目采用 **SaaS 授权架构** 与 **MongoDB 云端数据库**，支持跨频道的统一虚拟经济系统。

### ⚠️ 注意事项
> 该项目部分代码、注释和 Readme 使用 AI 辅助生成。如有 Bug 或建议，请提交 issue。

## ✨ 核心功能特性

**🔫 CS2 (Counter-Strike 2) 模块**
- 📈 实时饰品价格查询与全网比价 (Skinport API)
- 🎰 全真 CS2 模拟开箱引擎（支持基于官方掉落表的十连抽）
- 📊 玩家官匹战绩聚合查询与 Rating 2.0 数据估算分析
- 🏆 HLTV 实时电竞赛事状态与比分抓取

**🛸 APEX 英雄模块**
- 🗺️ 实时获取普通匹配与排位赛的地图轮换状态
- 🏅 跨平台 (PC/PS4/X1) 玩家战绩、段位与当前状态查询
- 🔴 APEX 组合包模拟器（传家宝概率实测验证）

**💼 商业与经济系统**
- 🏦 **跨游戏统一账户**：基于 MongoDB 的原子操作，支持高并发，绝不丢档。
- 🔑 **SaaS 动态授权门禁**：内置 `/auth` 系统，管理员可全自动管控各频道的开箱权限及有效期。

---

## ⚙️ 部署与配置说明

本项目的数据持久化**强依赖于 MongoDB**。所有敏感配置均通过**环境变量 (Environment Variables)** 读取，以保证极高的数据安全。

在运行本项目之前，你必须配置以下环境变量：

### 必填项 (核心驱动)
* `BOT_TOKEN` : 你的 KOOK 机器人 Token (在 KOOK 开发者后台获取)
* `MONGO_URI` : MongoDB 连接密钥 (例如: `mongodb+srv://<账号>:<密码>@cluster0...`)

### 选填项 (功能扩展)
* `OWNER_ID` : 你的 KOOK 账号数字 ID (用于使用 `/auth` 派发权限，**强烈建议填写**)
* `STEAM_API_KEY` : Steam 开发者 API 密钥 (用于 CS2 战绩查询与服务器探活)
* `APEX_API_KEY` : Apex Legends Status 开发者密钥 (用于地图与 APEX 战绩查询)
* `HF_TOKEN` : Hugging Face Access Token (用于翻译词库的云端拉取与缓存)
* `HF_REPO_ID` : 你的云端数据集仓库名 (配合 HF_TOKEN 使用，例如 `用户名/Bot_Data`)

### 🚀 部署方式 A：云原生部署 (推荐 Hugging Face Spaces / Zeabur / Railway)
本项目为无状态容器设计，完美兼容各类 Serverless 平台。直接在云平台的控制台中，找到 **Variables and Secrets (环境变量与密钥)** 设置页，将上述变量键值对添加进去，构建即可运行。

### 💻 部署方式 B：本地环境运行
如果你在本地电脑或私人服务器上运行，请将项目根目录的 `.env.example` 重命名为 `.env` 并填入你的配置，然后运行：
```bash
pip install -r requirements.txt
python app.py
