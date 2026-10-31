# 模糊指令评测集

116 句口语指令，打字/语音两种模式，逐句带期望（确定性路由、工具集、方案卡、界面效果、偏好、回复形态）。
打字回合经 `turn_router.classify_turn` 确定性路由：界面句直接执行无模型回合，开仓/平仓/看行情只给收窄工具集（temperature 0）。

运行方法见 `fuzzy_eval.py` 文件头：本机起真实 Runtime（native + deepseek，fake 数据），
`uv run --project . python eval/fuzzy/fuzzy_eval.py`，环境变量 `RUNTIME/TOKEN/DB/RUNS/ONLY/MODES`。
结果写本目录 `results.json` / `results.md`（勿提交）。

全集路由分类断言（无需模型）在 `tests/test_turn_router.py`。
