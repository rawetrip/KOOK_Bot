# KOOK Bot
基于 KOOK 平台和 Python 开发的 CS2 数据与娱乐机器人。

## 功能特性
- 实时饰品价格查询 (Skinport API)
- 全真 CS2 模拟开箱与全局经济账本
- 玩家官匹战绩查询与 Rating 2.0 数据分析
- HLTV 实时比赛状态获取

## 运行环境
数据持久化依赖 Hugging Face Spaces Dataset。需要在环境变量中配置 `BOT_TOKEN`, `STEAM_API_KEY`, 和 `HF_TOKEN`。

## ⚙️ 部署与配置说明

本项目的所有敏感配置均通过**环境变量 (Environment Variables)** 读取，以保证数据安全。在运行本项目之前，你需要配置以下环境变量：

* `BOT_TOKEN` : 你的 KOOK 机器人 Token (在 KOOK 开发者后台获取)
* `STEAM_API_KEY` : 你的 Steam 开发者 API 密钥
* `HF_TOKEN` : (可选) 你的 Hugging Face Access Token，需具备 Write 权限
* `HF_REPO_ID` : (可选) 你的云端数据集仓库名，例如 `用户名/Bot_Data`
* `ALLOWED_CHANNEL_ID` : 允许使用 `/open` 等刷屏指令的 KOOK 频道 ID

### 部署方式 A：云平台部署 (推荐 Hugging Face Spaces / Zeabur / Railway)
直接在云平台的控制台中，找到 **Variables and Secrets (环境变量与密钥)** 设置页，将上述变量名和你的实际值依次添加进去即可，代码会自动读取。

### 部署方式 B：本地环境运行
如果你在本地电脑或云服务器上运行，请把 `.env.example` 重命名为 `.env` 并填入你的配置：
```text
BOT_TOKEN=你的kook机器人token写这里
STEAM_API_KEY=你的steam_api_key写这里
HF_TOKEN=你的hf_token写这里
HF_REPO_ID=your_name/Bot_Data
ALLOWED_CHANNEL_ID=你的频道ID

该项目注释和Readme是使用AI生成的 如果有问题的话可以提交issue
