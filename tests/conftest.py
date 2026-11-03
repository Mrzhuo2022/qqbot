import nonebot

# 所有插件/共享模块都依赖 nonebot 运行时，先于测试导入完成初始化
nonebot.init()
