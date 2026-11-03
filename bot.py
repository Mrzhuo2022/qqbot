import logging
import os
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import LoguruHandler

# .env/data/插件目录都用相对路径，锚定到脚本所在目录，
# 从任意工作目录启动（服务、计划任务、IDE）都能找对位置
os.chdir(Path(__file__).resolve().parent)

# 插件里用的是标准 logging，接到 nonebot 的日志格式上才有输出
logging.basicConfig(level=logging.INFO, handlers=[LoguruHandler()], force=True)
# uvicorn 有自己的 handler，屏蔽其向 root 传播避免重复打印
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(name).propagate = False

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugins("src/plugins")

# 这两个导入依赖 nonebot.init 完成后的配置，必须留在 init 之后（E402 在 pyproject 豁免）
from src.shared import chat_store
from src.shared.config import plugin_config

# 会话存储：建库/迁移旧 chat_history.json，启动时清一次过期会话
chat_store.init()
if plugin_config.llm_history_ttl_days > 0:
    removed = chat_store.prune(plugin_config.llm_history_ttl_days)
    if removed:
        logging.info(
            "启动清理：删除 %d 个超过 %d 天未活跃的会话/群上下文",
            removed, plugin_config.llm_history_ttl_days,
        )

# 首次部署容易漏配的项，启动时直接点出来
from src.shared.config import log_config_warnings
from src.shared.storage import legacy_group_offs

log_config_warnings()

# 旧版 /bot off 命令写进 settings.json 的群级开关已改由 .env 的
# group_plugin_off 管理；有旧记录时点出来，提醒搬到配置里
_legacy = legacy_group_offs()
if _legacy:
    logging.warning(
        "settings.json 存在旧版群级开关 %s：群级开关已改由 .env 的 "
        "group_plugin_off 配置（重启生效），这些旧记录不再生效",
        _legacy,
    )

if __name__ == "__main__":
    nonebot.run()
