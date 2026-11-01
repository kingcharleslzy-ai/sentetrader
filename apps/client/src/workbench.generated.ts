// Generated from Runtime routes by npm run generate:workbench-api. Do not edit.
export interface paths {
    "/api/app/strategy-explanations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Explain */
        post: operations["explain_api_app_strategy_explanations_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/strategy-explanations/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Explanation */
        get: operations["explanation_api_app_strategy_explanations__id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/windows/{window_id}/captures": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Request Capture */
        post: operations["request_capture_api_app_ui_windows__window_id__captures_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/captures/{request_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Capture */
        get: operations["read_capture_api_app_ui_captures__request_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/ui/pulse": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Pulse */
        post: operations["pulse_api_ui_pulse_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/windows": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Windows */
        get: operations["list_windows_api_app_ui_windows_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/windows/{window_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Window Detail */
        get: operations["window_detail_api_app_ui_windows__window_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Enqueue */
        post: operations["enqueue_api_app_ui_commands_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/ui/commands/{command_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Command Status */
        get: operations["command_status_api_app_ui_commands__command_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/push": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Push Status
         * @description 锁屏提醒：服务端是否已配置、VAPID 公钥，和本人已开锁屏提醒的设备（deviceId、设备名、开启时间、
         *     最近一次送达与失败原因）。
         */
        get: operations["push_status_api_app_push_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/push/subscription": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /**
         * Save Push Subscription
         * @description 本设备开启锁屏提醒：保存浏览器给的推送订阅（必须由用户在页面上点按钮、浏览器授权后得到）。
         *     谷歌 FCM 的订阅（Chrome、安卓）国内送不到，422 push_endpoint_unsupported；其他非推送服务地址
         *     422 push_endpoint_invalid；服务端未配置推送 409 push_not_configured。
         */
        put: operations["save_push_subscription_api_app_push_subscription_put"];
        post?: never;
        /**
         * Delete Push Subscription
         * @description 移除一台设备的锁屏提醒（本机关闭或在设备列表里移除另一台）。
         */
        delete: operations["delete_push_subscription_api_app_push_subscription_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/push/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Test Push
         * @description 发一条试试：给本人所有已开锁屏提醒的设备（或 deviceId 指定的一台）发一条“Sente 试一条”，
         *     返回每台的结果（sent / removed＝推送服务说订阅已失效、已删除 / failed）。不受“通知哪些事”和
         *     页面是否在前台影响。服务端未配置推送 409 push_not_configured。
         */
        post: operations["test_push_api_app_push_test_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/preferences": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Preferences */
        get: operations["read_preferences_api_app_preferences_get"];
        /** Save Preferences */
        put: operations["save_preferences_api_app_preferences_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/help": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * App Help
         * @description 使用说明：分组、旧 id 别名与主题；topic（可用旧 id）只返回该主题。
         */
        get: operations["app_help_api_app_help_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/activity": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** App Activity */
        get: operations["app_activity_api_app_activity_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/config": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** App Config */
        get: operations["app_config_api_app_config_get"];
        /** Save App Config */
        put: operations["save_app_config_api_app_config_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/briefs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * App Briefs
         * @description AI 简报：带 symbol 只读该合约（1.6.x 契约）；不带 symbol 合并读取全部订阅品种
         *     （新闻 + 异动，按时间新到旧，每条带 product/productName/symbol）。
         */
        get: operations["app_briefs_api_app_briefs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/briefs/refresh": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Refresh App Briefs */
        post: operations["refresh_app_briefs_api_app_briefs_refresh_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/brief-subscriptions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * App Brief Subscriptions
         * @description AI 简报的品种订阅：生效品种（含中文名与当前主力合约）、档位上限（null=不限）与来源
         *     （default=默认跟随自选/主力；user=用户自选）。可选 symbol（当前主力合约）只在既无自选、
         *     也未设置过订阅时用于推导默认品种。
         */
        get: operations["app_brief_subscriptions_api_app_brief_subscriptions_get"];
        /**
         * Save App Brief Subscriptions
         * @description 设置 AI 简报订阅品种（有序、去重；空列表=不订阅）。未知品种 422 brief_product_unknown，
         *     超出档位上限 422 brief_limit_exceeded，均整体拒绝不落库；档位调低不删除已存设置。
         */
        put: operations["save_app_brief_subscriptions_api_app_brief_subscriptions_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/app/config/rollback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Rollback App Config */
        post: operations["rollback_app_config_api_app_config_rollback_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/guide": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Retired Guide Get Guide
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        get: operations["retired_guide_get_guide_api_strategy_plans_guide_get"];
        put?: never;
        /**
         * Retired Guide Post Guide
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        post: operations["retired_guide_post_guide_api_strategy_plans_guide_post"];
        /**
         * Retired Guide Delete Guide
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        delete: operations["retired_guide_delete_guide_api_strategy_plans_guide_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/guide/answer": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Retired Guide Post Answer
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        post: operations["retired_guide_post_answer_api_strategy_plans_guide_answer_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/guide/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Retired Guide Post Confirm
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        post: operations["retired_guide_post_confirm_api_strategy_plans_guide_confirm_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/guide/pause": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Retired Guide Post Pause
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        post: operations["retired_guide_post_pause_api_strategy_plans_guide_pause_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/guide/undo": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Retired Guide Post Undo
         * @deprecated
         * @description AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。
         */
        post: operations["retired_guide_post_undo_api_strategy_plans_guide_undo_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/trading-costs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Costs */
        get: operations["get_costs_api_trading_costs_get"];
        put?: never;
        /** Save Costs */
        post: operations["save_costs_api_trading_costs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/workspace": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Workspace Identity */
        get: operations["workspace_identity_api_workspace_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/design-jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Start Design */
        post: operations["start_design_api_strategy_plans_design_jobs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/design-jobs/{identifier}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Design Status */
        get: operations["design_status_api_strategy_plans_design_jobs__identifier__get"];
        put?: never;
        post?: never;
        /** Stop Design */
        delete: operations["stop_design_api_strategy_plans_design_jobs__identifier__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/design": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Design
         * @deprecated
         * @description 兼容同步入口；交互式客户端请使用design-jobs创建可恢复的生成任务。
         */
        post: operations["design_api_strategy_plans_design_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Library */
        get: operations["library_api_strategy_plans_get"];
        put?: never;
        /**
         * Save Plan
         * @description 保存一条策略库版本。策略编辑卡 (2026-10-08): baseStrategyId = 修改的是哪条——存为它的新版本（同名时
         *     版本号加一，原版本保留），它定义里卡片以外的字段（模板来源等）原样带上；templateId = 新建时的模板起点，
         *     记为模板来源（和模板默认不同时标为已调整），与已有策略重名时加“(2)”等序号。都不带时照旧：同名存为
         *     下一个版本，同样的定义再存返回原来那条。
         */
        post: operations["save_plan_api_strategy_plans_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/prefill": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Habit Prefill Route
         * @description 策略编辑卡“按我的习惯”(2026-10-08): 从本账户近 20 个交易日的已平仓成交（与复盘同一条成交轮次）
         *     按统计算出能确定的字段——合约（最常做的品种的主力）、方向（一侧占比 ≥70%）、止损跳数（该品种亏损单
         *     出场跳数的中位数，至少 3 笔亏损单）、收盘前平仓（都没留隔夜）、开仓时段（≥70% 在同一连续时段）、
         *     每次手数（众数）；每项带一句依据和成交 id。不到 5 笔不预填（eligible: false 与原因）。开仓条件
         *     不预填。只读这次请求所在的账户，不调模型、不下单。
         */
        get: operations["habit_prefill_route_api_strategy_plans_prefill_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/{identifier}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Delete Plan */
        delete: operations["delete_plan_api_strategy_plans__identifier__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/{identifier}/scope": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Update Scope */
        post: operations["update_scope_api_strategy_plans__identifier__scope_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/{identifier}/apply-advice": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Apply Health Advice
         * @description 按这个改 (策略体检, 2026-09-26): one structural suggestion from this strategy's rule backtest
         *     (drop one entry condition / let the crosses happen within 3 bars) applied to its saved plan and
         *     saved as the next version of the same name, like 改一版 — the original version stays. Returns
         *     the saved strategy. Never trades, validates or starts monitoring.
         */
        post: operations["apply_health_advice_api_strategy_plans__identifier__apply_advice_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/chart-period": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Chart Period */
        post: operations["chart_period_api_strategy_plans_chart_period_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/{identifier}/validate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Validate Plan */
        post: operations["validate_plan_api_strategy_plans__identifier__validate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-plans/{identifier}/conditions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Conditions */
        get: operations["conditions_api_strategy_plans__identifier__conditions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategies/{strategy_id}/backtest": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Backtest Strategy
         * @description 用这条策略自己的规则回测历史（与上线运行同一套判定）：信号按收盘确认、下一根开盘成交，
         *     止损止盈按K线高低价判断，计入“设置 → 交易”的手续费。只读取历史行情、只返回结果，
         *     不下单；结果存库（每条策略最近 5 次，GET /api/strategy-backtests），在服务端算（source: server）。
         */
        post: operations["backtest_strategy_api_strategies__strategy_id__backtest_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-backtests": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Recent Backtests
         * @description 本人最近的规则回测结果（存库：每条策略留最近 5 次，未保存的方案与模板预览共留 5 次；新的在前，
         *     重启不丢）。strategyId 只看这条策略的；存下的逐笔清单只留最近 300 笔（摘要按全部成交算）。
         */
        get: operations["recent_backtests_api_strategy_backtests_get"];
        put?: never;
        /**
         * Backtest Plan
         * @description 不保存也能回测：body 带完整策略定义与合约（AI 比较“改前 / 改后”用）。口径同
         *     POST /api/strategies/{id}/backtest；只读，不保存策略。preview（策略编辑卡的实时回测）只算不存，
         *     结果另带 planSummary（这份计划的一句话）；diagnosis 里每个开仓组有各条件单独满足次数与组的
         *     同时满足（allPass）/ 任一满足（anyPass）次数。
         */
        post: operations["backtest_plan_api_strategy_backtests_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-backtests/inputs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Browser Inputs
         * @description 本机回测的输入（界面默认在本机浏览器里算，2026-10-12）：这次回测要读的全部——K线、合约乘数与最小
         *     变动、手续费（同服务端口径）、交易时段、初始资金、滑点——一次给齐（gzip），带 inputDigest 与
         *     engineDigest。给 plan 按这份定义（同 POST /api/strategy-backtests），只给 strategyId 按已存策略（同
         *     POST /api/strategies/{id}/backtest）。只读历史、不计算，但读历史占一个回测队列名额（同服务端回测的并发上限，排队超时 429）；
         *     服务端记下这次输入 15 分钟（返回 ticketId），算完用 POST /api/strategy-backtests/browser-result 交回。AI 与 CLI 直接用服务端回测接口，不用这一对。
         */
        post: operations["browser_inputs_api_strategy_backtests_inputs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-backtests/browser-result": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Browser Result
         * @description 交回本机算完的回测报告：服务端按发出输入时记下的策略、合约与参数包装成与服务端回测同样的结果
         *     （backtestId、AI 怎么看的事实句；非 preview 的存库），带 source: browser 与两个摘要。引擎已更新返回
         *     409 engine_changed（刷新页面后重跑），这次输入服务端已不记得（重启、超过 15 分钟）返回 409
         *     inputs_expired（改走服务端回测）。每次 inputs 发一张带 ticketId 的票据，同一张票据重复交回（回包丢了
         *     的重试）返回第一次的同一份结果，不会再存一条、再发一张卡。本机结果只用于展示：存为策略后的回测由
         *     服务端重跑。
         */
        post: operations["browser_result_api_strategy_backtests_browser_result_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-backtests/{backtest_id}/verdict": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Backtest Verdict
         * @description 回测结果的“AI 怎么看”：程序先从这次结果算出事实句（最赚钱那个月占多少、去掉它还剩多少、笔数
         *     太少、没扣手续费、参数是不是在同一段行情里挑的），再请模型用一两句大白话说出来；模型的话里每个
         *     数字都必须是事实句里的数字，不符、没有模型或出错时只给事实句（source: facts）。写好后存回这次
         *     结果，再调直接返回存下的那句。只读回测，不改策略、不下单。
         */
        post: operations["backtest_verdict_api_strategy_backtests__backtest_id__verdict_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-backtests/queue": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Backtest Queue
         * @description 本人正在排队或运行的规则回测：state queued / running，ahead = 前面还有几个（含正在运行的）。
         *     默认全局同时最多 2 个、每人 1 个（部署可用 AI_TRADER_BACKTEST_* 调整，当前值见 limits），先到先跑；
         *     排队超过等待时限（默认 60 秒）放弃并返回 429“回测排队中，稍后再试”。
         */
        get: operations["backtest_queue_api_strategy_backtests_queue_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Templates
         * @description 模板库：分类、一句话场景、大白话 plainSummary、level（新手 / 进阶）、holding（日内 / 会隔夜）、来源、推荐执行周期、
         *     可调参数表（范围、步长、映射到 plan 的字段、help = {what, up, down} 大白话说明）。
         */
        get: operations["list_templates_api_strategy_templates_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-templates/{template_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Template
         * @description 一个模板的完整定义：列表字段 + 默认参数 + 按默认参数实例化的完整策略定义（plan）。
         */
        get: operations["get_template_api_strategy_templates__template_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-templates/{template_id}/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Preview Template
         * @description 按参数实例化（不保存），并在 symbol 上跑一次规则回测（口径同 POST /api/strategy-backtests）。
         *     参数越界返回 422 与中文原因。只读，不下单、不保存策略。
         */
        post: operations["preview_template_api_strategy_templates__template_id__preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/strategy-templates/{template_id}/instantiate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Instantiate Template
         * @description 按参数创建一条策略库草稿（与 POST /api/strategy-plans 同一流程），definition.templateSource
         *     记录来源模板与参数；同一定义重复创建返回已有的那条。返回保存的策略（含 strategyId）。
         */
        post: operations["instantiate_template_api_strategy_templates__template_id__instantiate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/trade-review": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Trade Review */
        get: operations["trade_review_api_trade_review_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** ActivityFill */
        ActivityFill: {
            /** Tradeid */
            tradeId: string;
            /** Clientorderid */
            clientOrderId: string;
            /** Symbol */
            symbol: string;
            /** Direction */
            direction: string;
            /** Offset */
            offset: string;
            /** Price */
            price: number | null;
            /** Volume */
            volume: number;
            /** Tradedatetime */
            tradeDateTime: number;
            /**
             * Source
             * @enum {string}
             */
            source: "ai" | "stop";
            /** Strategyid */
            strategyId?: string | null;
        } & {
            [key: string]: unknown;
        };
        /** ActivityOrder */
        ActivityOrder: {
            /** Id */
            id: string;
            /** Proposalid */
            proposalId: string;
            /** Clientorderid */
            clientOrderId: string;
            request: components["schemas"]["OrderRequest"];
            /**
             * Submitphase
             * @enum {string}
             */
            submitPhase: "PREPARED" | "CALL_STARTED" | "UNKNOWN" | "CONFIRMED" | "ABANDONED";
            /**
             * Cancelphase
             * @enum {string}
             */
            cancelPhase: "NONE" | "CALL_STARTED" | "UNKNOWN" | "CONFIRMED";
            /** Displaystatus */
            displayStatus: string;
            /** Filledvolume */
            filledVolume: number;
            /** Reason */
            reason: string;
            /**
             * Source
             * @enum {string}
             */
            source: "ai" | "stop";
            /** Createdat */
            createdAt: string;
            /** Updatedat */
            updatedAt: string;
            /** Strategyid */
            strategyId?: string | null;
        } & {
            [key: string]: unknown;
        };
        /** ActivitySnapshot */
        ActivitySnapshot: {
            /**
             * Environment
             * @enum {string}
             */
            environment: "replay" | "simulation";
            /** Replayid */
            replayId: string | null;
            /** Symbol */
            symbol: string;
            /** Capturedat */
            capturedAt: string | null;
            /** Datastatus */
            dataStatus: string | null;
            /** Ageseconds */
            ageSeconds: number | null;
            /** Orders */
            orders: components["schemas"]["ActivityOrder"][];
            /** Fills */
            fills: components["schemas"]["ActivityFill"][];
            /** Strategies */
            strategies: components["schemas"]["ActivityStrategy"][];
            /** Protections */
            protections?: {
                [key: string]: unknown;
            }[] | null;
            /** Foreignaccount */
            foreignAccount?: {
                [key: string]: unknown;
            } | null;
        };
        /** ActivityStrategy */
        ActivityStrategy: {
            /** Strategyid */
            strategyId: string;
            /** Name */
            name: string;
            /** Status */
            status: string;
            decision: components["schemas"]["StrategyDecision"] | null;
            monitorState: components["schemas"]["MonitorState"] | null;
            /** Recentattempts */
            recentAttempts: {
                [key: string]: unknown;
            }[];
        };
        /** ApplyAdviceRequest */
        ApplyAdviceRequest: {
            advice: components["schemas"]["HealthAdvice"];
        };
        /**
         * AtrStop
         * @description 入场止损距离 = 信号K线 ATR × multiple；成交后按成交价换算成固定止损价。
         */
        AtrStop: {
            /** Multiple */
            multiple: number;
            /**
             * Period
             * @default 14
             */
            period: number;
        };
        /**
         * BacktestOptions
         * @description 规则回测的参数；都可省略：合约默认策略自己的合约，区间默认按周期取可取到的最近 N 根。
         */
        BacktestOptions: {
            /**
             * Symbol
             * @description 回测合约；省略时用策略绑定的合约，策略库版本须指定
             */
            symbol?: string | null;
            /**
             * Timeframe
             * @description 跟随图表周期的策略用哪个周期回测
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /**
             * Tradingdays
             * @description 只回测最近 N 个交易日；超过数据上限时用全部可取到的历史
             */
            tradingDays?: number | null;
            /**
             * Slippageticks
             * @description 开平各算几跳滑点，默认 0
             * @default 0
             */
            slippageTicks: number;
            /**
             * Initialbalance
             * @description 初始资金；省略时取账户权益，读不到用 100 万
             */
            initialBalance?: number | null;
            /**
             * Sessionid
             * @description 把结果卡发到这个对话
             */
            sessionId?: string | null;
            /**
             * Requestid
             * @description 调用方自取的请求号；排队时用 GET /api/strategy-backtests/queue?requestId= 查看位置
             */
            requestId?: string | null;
            /**
             * Fallbackreason
             * @description 界面本机优先时为什么改在服务端算（排障用，随结果存下）；AI 与 CLI 不用传
             */
            fallbackReason?: ("unsupported" | "device" | "warming" | "init_failed" | "init_timeout" | "memory" | "crashed" | "engine_error" | "hidden" | "slow" | "inputs_failed" | "inputs_expired" | "rerun") | null;
        };
        /**
         * BoardSortChange
         * @description 报价总表: which column the board sorts by and in which direction.
         */
        BoardSortChange: {
            /**
             * Key
             * @enum {string}
             */
            key: "changePercent" | "volume" | "openInterest" | "openInterestChange";
            /**
             * Order
             * @enum {string}
             */
            order: "desc" | "asc";
        };
        /**
         * Branch
         * @description 条件组合的一个分支：一小组条件，组内按 join 合并，整体当作本组的一条条件。
         */
        Branch: {
            /**
             * Join
             * @default all
             * @enum {string}
             */
            join: "all" | "any";
            /** Conditions */
            conditions: components["schemas"]["Condition"][];
        };
        /**
         * Breakeven
         * @description 保本：浮盈（持仓后最高/最低价）达 triggerTicks 跳或 triggerAtr×ATR14 后，止损移到开仓价±offsetTicks 跳，只收紧。
         */
        Breakeven: {
            /** Triggerticks */
            triggerTicks?: number | null;
            /** Triggeratr */
            triggerAtr?: number | null;
            /**
             * Offsetticks
             * @default 0
             */
            offsetTicks: number;
        };
        /** Brief */
        Brief: {
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "market" | "news";
            /** Text */
            text: string;
            /** Source */
            source: string;
            /** Url */
            url?: string | null;
            /** Publishedat */
            publishedAt: string;
            /** Expiresat */
            expiresAt: string;
            /** Eventat */
            eventAt?: string | null;
            /** Product */
            product?: string | null;
            /** Productname */
            productName?: string | null;
            /** Symbol */
            symbol?: string | null;
        };
        /** BriefRefresh */
        BriefRefresh: {
            /** Symbol */
            symbol?: string | null;
        };
        /** BriefRefreshResult */
        BriefRefreshResult: {
            /**
             * Status
             * @enum {string}
             */
            status: "disabled" | "unavailable" | "idle" | "pending";
            /** Product */
            product?: string | null;
        };
        /**
         * BriefSubscriptionItem
         * @description One subscribed product with its Chinese name and the product's current
         *     main contract (null when the board read could not resolve it).
         */
        BriefSubscriptionItem: {
            /** Product */
            product: string;
            /** Name */
            name: string;
            /** Symbol */
            symbol?: string | null;
        };
        /** BriefSubscriptionsSnapshot */
        BriefSubscriptionsSnapshot: {
            /** Products */
            products: components["schemas"]["BriefSubscriptionItem"][];
            /** Limit */
            limit: number | null;
            /**
             * Source
             * @enum {string}
             */
            source: "default" | "user";
            /**
             * Overlimit
             * @default false
             */
            overLimit: boolean;
        };
        /** BriefSubscriptionsUpdate */
        BriefSubscriptionsUpdate: {
            /** Products */
            products: string[];
        };
        /** BriefsSnapshot */
        BriefsSnapshot: {
            /** Items */
            items: components["schemas"]["Brief"][];
            /** Status */
            status?: string | null;
        };
        /**
         * BrowserInputsRequest
         * @description 本机回测要的输入：给 plan 按这份定义（同 POST /api/strategy-backtests），不给 plan 给 strategyId 按这条
         *     已存策略（同 POST /api/strategies/{id}/backtest）。
         */
        BrowserInputsRequest: {
            /**
             * Symbol
             * @description 回测合约；省略时用策略绑定的合约，策略库版本须指定
             */
            symbol?: string | null;
            /**
             * Timeframe
             * @description 跟随图表周期的策略用哪个周期回测
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /**
             * Tradingdays
             * @description 只回测最近 N 个交易日；超过数据上限时用全部可取到的历史
             */
            tradingDays?: number | null;
            /**
             * Slippageticks
             * @description 开平各算几跳滑点，默认 0
             * @default 0
             */
            slippageTicks: number;
            /**
             * Initialbalance
             * @description 初始资金；省略时取账户权益，读不到用 100 万
             */
            initialBalance?: number | null;
            /**
             * Sessionid
             * @description 把结果卡发到这个对话
             */
            sessionId?: string | null;
            /**
             * Requestid
             * @description 调用方自取的请求号；排队时用 GET /api/strategy-backtests/queue?requestId= 查看位置
             */
            requestId?: string | null;
            /**
             * Fallbackreason
             * @description 界面本机优先时为什么改在服务端算（排障用，随结果存下）；AI 与 CLI 不用传
             */
            fallbackReason?: ("unsupported" | "device" | "warming" | "init_failed" | "init_timeout" | "memory" | "crashed" | "engine_error" | "hidden" | "slow" | "inputs_failed" | "inputs_expired" | "rerun") | null;
            plan?: components["schemas"]["StrategyPlan"] | null;
            /**
             * Strategyid
             * @description 已存策略；给了 plan 时只作标识
             */
            strategyId?: string | null;
            /**
             * Preview
             * @description 策略编辑卡的实时回测：只算不存
             * @default false
             */
            preview: boolean;
        };
        /**
         * BrowserResultRequest
         * @description 浏览器按 inputs 算完的回测报告；服务端按自己记下的那次输入包装、存档。
         */
        BrowserResultRequest: {
            /**
             * Inputdigest
             * @description POST /api/strategy-backtests/inputs 给的 inputDigest
             */
            inputDigest: string;
            /**
             * Ticketid
             * @description POST /api/strategy-backtests/inputs 给的 ticketId（这次输入的票据）
             */
            ticketId: string;
            /**
             * Enginedigest
             * @description 算这次结果的引擎摘要
             */
            engineDigest: string;
            /**
             * Result
             * @description run_rule 的报告（summary、coverage、trades 等）
             */
            result: {
                [key: string]: unknown;
            };
            /**
             * Runms
             * @description 浏览器里计算用了多少毫秒
             */
            runMs?: number | null;
        };
        /** CaptureReceipt */
        CaptureReceipt: {
            /**
             * Requestid
             * Format: uuid
             */
            requestId: string;
            /** Observedrevision */
            observedRevision: number;
            /** Capturedat */
            capturedAt: string;
            image?: components["schemas"]["CapturedImage"] | null;
            /** Error */
            error?: ("private_window" | "page_changed" | "capture_unavailable") | null;
            /** Detail */
            detail?: string | null;
        };
        /** CaptureRequest */
        CaptureRequest: {
            /**
             * Requestid
             * Format: uuid
             */
            requestId: string;
            /** Expectedrevision */
            expectedRevision: number;
        };
        /** CaptureResult */
        CaptureResult: {
            /**
             * Requestid
             * Format: uuid
             */
            requestId: string;
            /** Expectedrevision */
            expectedRevision: number;
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "dispatched" | "ready" | "rejected" | "expired";
            /** Capturedat */
            capturedAt?: string | null;
            image?: components["schemas"]["CapturedImage"] | null;
            /** Error */
            error?: string | null;
        };
        /** CapturedImage */
        CapturedImage: {
            /**
             * Mimetype
             * @enum {string}
             */
            mimeType: "image/png" | "image/jpeg";
            /** Data */
            data: string;
        };
        /**
         * ChartAnnotationItem
         * @description One drawing on a chart pane. Times are bar timestamps in epoch seconds (the chart's own unit,
         *     `chartViewport.timeRange` in the snapshot); `price` is the pane's value unit (contract price on
         *     main, K/D on kd, DIF/DEA/柱 on macd, 0–100 on rsi, 手 on oi). `point` is the precise
         *     entry/exit/close marker (role picks the glyph and tone); hline/zone are for levels and areas
         *     only. Labels are ≤8 chars: the chart shows the item's number ①②③ plus this label, the
         *     explanation goes in the reply.
         */
        ChartAnnotationItem: {
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "point" | "hline" | "zone" | "trendline" | "vline" | "circle" | "arrow" | "text";
            /**
             * Pane
             * @default main
             * @enum {string}
             */
            pane: "main" | "kd" | "macd" | "rsi" | "oi" | "cci";
            /**
             * Role
             * @description point 用：buy 上三角、sell 下三角、close 方块、note 圆点
             */
            role?: ("buy" | "sell" | "close" | "note") | null;
            /** Price */
            price?: number | null;
            /** Price2 */
            price2?: number | null;
            /** Time */
            time?: number | null;
            /** Time2 */
            time2?: number | null;
            /**
             * Label
             * @description ≤8 字短标签，如“空 15420”；解释写在回复里按编号讲
             */
            label?: string | null;
            /**
             * Tone
             * @description 省略时 point 按 role 取色（buy=good、sell=danger、close=warn、note=info），其余 info
             */
            tone?: ("info" | "warn" | "danger" | "good") | null;
        };
        /**
         * ChartAnnotationsChange
         * @description `set` replaces, `add` appends (same id replaces), `clear` removes the AI's drawings on one contract
         *     (default: the window's current contract). At most 6 items per contract — one screen stays readable.
         */
        ChartAnnotationsChange: {
            /**
             * Op
             * @enum {string}
             */
            op: "set" | "add" | "clear";
            /** Items */
            items?: components["schemas"]["ChartAnnotationItem"][];
            /** Symbol */
            symbol?: string | null;
        };
        /** ChartExtension */
        ChartExtension: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "sma" | "ema" | "price_channel";
            /** Period */
            period: number;
            /**
             * Color
             * @default #8d9eff
             */
            color: string;
            /**
             * Enabled
             * @default true
             */
            enabled: boolean;
        };
        /**
         * ChartFocusChange
         * @description Scroll the chart to a time span (epoch seconds, bar timestamps); `null` returns to the live view.
         */
        ChartFocusChange: {
            /** From */
            from: number;
            /** To */
            to: number;
        };
        /** ChartOptionChange */
        ChartOptionChange: {
            /**
             * Name
             * @enum {string}
             */
            name: "kd" | "macd" | "volume" | "crosshair" | "lastPriceLine" | "ma5" | "ma10" | "ma25" | "ma60" | "ma200";
            /** Visible */
            visible: boolean;
        };
        /** ChartOptions */
        ChartOptions: {
            /** Kd */
            kd: boolean;
            /** Macd */
            macd: boolean;
            /** Volume */
            volume: boolean;
            /** Crosshair */
            crosshair: boolean;
            /** Lastpriceline */
            lastPriceLine: boolean;
            /** Ma5 */
            ma5: boolean;
            /** Ma10 */
            ma10: boolean;
            /** Ma25 */
            ma25: boolean;
            /** Ma60 */
            ma60: boolean;
            /** Ma200 */
            ma200: boolean;
        };
        /** ChartPeriodRequest */
        ChartPeriodRequest: {
            /** Symbol */
            symbol: string;
            /**
             * Timeframeseconds
             * @enum {integer}
             */
            timeframeSeconds: 60 | 180 | 300 | 900 | 3600 | 86400;
        };
        /** ChartSizes */
        ChartSizes: {
            /** Main */
            main: number;
            /** Kd */
            kd: number;
            /** Macd */
            macd: number;
        };
        /** ChatHistoryItem */
        ChatHistoryItem: {
            /** Sessionid */
            sessionId: string;
            /** Createdat */
            createdAt: string;
        };
        /**
         * ChatSessionChange
         * @description Switch the AI dock to a chat session: `sessionId` from `chatHistory`, or `new: true` for a fresh one.
         */
        ChatSessionChange: {
            /** Sessionid */
            sessionId?: string | null;
            /**
             * New
             * @default false
             */
            new: boolean;
        };
        /** Clarification */
        Clarification: {
            /** Id */
            id: string;
            /** Question */
            question: string;
            /** Options */
            options: components["schemas"]["ClarificationOption"][];
            /**
             * Recommended
             * @default 0
             */
            recommended: number;
        };
        /** ClarificationOption */
        ClarificationOption: {
            /** Label */
            label: string;
            /** Description */
            description: string;
        };
        /** Condition */
        Condition: {
            /** Label */
            label: string;
            left: components["schemas"]["Operand"];
            /**
             * Operator
             * @enum {string}
             */
            operator: "gt" | "gte" | "lt" | "lte" | "cross_up" | "cross_down" | "rising" | "falling" | "eq";
            right: components["schemas"]["Operand"];
            /**
             * Withinbars
             * @default 1
             */
            withinBars: number;
            /**
             * Consecutive
             * @default 1
             */
            consecutive: number;
            /**
             * Mincount
             * @default 1
             */
            minCount: number;
        };
        /** ConfigRollback */
        ConfigRollback: {
            /** Revision */
            revision: number;
        };
        /** ConfigSnapshot */
        ConfigSnapshot: {
            /** Revision */
            revision: number;
            config: components["schemas"]["WorkbenchConfig"];
            /** Canrollback */
            canRollback: boolean;
        };
        /** ConfigUpdate */
        ConfigUpdate: {
            /** Revision */
            revision: number;
            config: components["schemas"]["WorkbenchConfig"];
        };
        /** DesignJobRequest */
        DesignJobRequest: {
            /** Description */
            description: string;
            /** Constraints */
            constraints?: {
                [key: string]: unknown;
            };
            /** Id */
            id?: string | null;
        };
        /** DesignRequest */
        DesignRequest: {
            /** Description */
            description: string;
            /** Constraints */
            constraints?: {
                [key: string]: unknown;
            };
        };
        /** DialogChange */
        DialogChange: {
            /**
             * Name
             * @enum {string}
             */
            name: "account-detail" | "fee-hint" | "market-session" | "help" | "contract-drawer" | "positions-drawer" | "replay-setup" | "resume-execution" | "emergency-stop" | "quick-order-confirm" | "abandon-order" | "strategy-designer" | "proposal-confirm" | "update-dialog" | "strategy-launch" | "change-password" | "protection-reprice-confirm";
            /** Open */
            open: boolean;
            /** Topic */
            topic?: string | null;
        };
        /** DisclosureChange */
        DisclosureChange: {
            /**
             * Name
             * @enum {string}
             */
            name: "feedback-diagnostics" | "activity-more" | "activity-history" | "timeline-history" | "timeline-expanded" | "attempt-history" | "designer-expanded" | "replay-fees" | "replay-design" | "protection-status" | "strategy-idea" | "fee-overrides" | "progress-steps" | "advice-sources" | "tool-card" | "diff-card" | "card-details" | "voice-call" | "voice-mute" | "voice-menu" | "voice-review" | "unlinked-notes" | "review-how" | "habits-how" | "chat-history" | "ai-command-result" | "activity-clear" | "strategy-guide" | "strategy-new" | "strategy-run-log" | "strategy-ask-ai" | "strategy-editor-trades" | "strategy-editor-more" | "strategy-editor-advanced" | "strategy-editor-backtest" | "strategy-editor-section" | "strategy-editor-condition" | "strategy-editor-picker" | "strategy-editor-sizing-more" | "help-tip" | "strategy-signal-action" | "strategy-stop-confirm" | "ai-menu" | "strategy-actions" | "strategy-delete-confirm" | "strategy-backtest-trades" | "strategy-param-help" | "strategy-template-details" | "strategy-advanced" | "strategy-full-rules" | "chart-ma-editor" | "chart-pane-editor" | "chart-pane-add" | "board-columns" | "price-alerts" | "reminder-alert-new" | "playbook-editor" | "price-alerts-others" | "position-protection" | "contract-info" | "flash-calendar" | "flash-dropped" | "news-dropped" | "flash-minor" | "news-minor" | "flash-event" | "news-event" | "flash-orig" | "news-orig" | "desk-fundamental" | "desk-macro" | "desk-reports" | "desk-sources" | "chart-drawing-tools" | "chart-drawing-toolbar" | "chart-drawing-group" | "chart-drawing-tool" | "chart-drawing-magnet" | "chart-drawing-lock-all" | "chart-drawings-hidden" | "chart-drawing-clear" | "chart-drawing-style" | "chart-drawing-settings" | "chart-context-menu" | "chart-periods-more" | "order-depth" | "ticket-protection" | "brief-subscriptions" | "brief-add" | "push-devices" | "routine-delete" | "plan-quota" | "live-trading" | "mobile-subpanes" | "mobile-subpanes-overlays" | "mobile-quote-columns" | "mobile-chart-cell" | "mobile-drawing-style-row" | "debate-sheets" | "debate-sources" | "debate-stats" | "debate-log" | "debate-judge" | "debate-analyst-technical" | "debate-analyst-supply" | "debate-analyst-flow" | "debate-analyst-news";
            /** Open */
            open: boolean;
            /** Id */
            id?: string | null;
        };
        /** EntryGroup */
        EntryGroup: {
            /**
             * Join
             * @default all
             * @enum {string}
             */
            join: "all" | "any";
            /**
             * Sequence
             * @default false
             */
            sequence: boolean;
            /** Conditions */
            conditions?: components["schemas"]["Condition"][];
            /**
             * Branches
             * @description 条件组合：每个分支先在组内按自己的join合并，再与conditions里的单条条件一起按本组join合并；例如 join=any、两个 all 分支 = (A且B)或(C且D)。
             */
            branches?: components["schemas"]["Branch"][];
            /**
             * Alignmentconditions
             * @description 入场方向状态，全部同向后等待conditions确认；方向被打断才开始新的一段，每段最多一次开仓尝试。
             */
            alignmentConditions?: components["schemas"]["Condition"][];
            /**
             * Rearm
             * @description 复位：本方向开仓后须等这些条件（执行周期、全部满足）成立一次才可再开。
             */
            rearm?: components["schemas"]["Condition"][];
            /** @description 盘中触价入场：条件在收盘成立后挂价（price 取这根收盘时的值），下一根K线内价格触及才开仓。 */
            trigger?: components["schemas"]["EntryTrigger"] | null;
        };
        /**
         * EntryTrigger
         * @description 盘中触价入场：本组条件在收盘成立时，按这根K线收盘时 price 的值挂一个触价单，只在下一根执行周期
         *     K线内有效——stop：多单价格涨到它（空单跌到它）就开仓，limit：多单价格回落到它（空单反弹到它）就开仓；
         *     没触及就作废，下一根收盘条件仍成立再按新价位挂。
         */
        EntryTrigger: {
            /**
             * Kind
             * @default stop
             * @enum {string}
             */
            kind: "stop" | "limit";
            price: components["schemas"]["Operand"];
        };
        /**
         * EntryWindow
         * @description 允许开仓的北京时间段（含两端，start>end 表示跨午夜）；出场不受限制。
         */
        EntryWindow: {
            /** Start */
            start: string;
            /** End */
            end: string;
        };
        /** ExplanationRequest */
        ExplanationRequest: {
            /** Symbol */
            symbol: string;
            /** Strategyid */
            strategyId?: string | null;
            /**
             * Eventtype
             * @enum {string}
             */
            eventType: "fill" | "attempt" | "protection" | "order";
            /** Eventid */
            eventId: string;
            /**
             * Retry
             * @default false
             */
            retry: boolean;
        };
        /** ExplanationResult */
        ExplanationResult: {
            /** Id */
            id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "waiting" | "analyzing" | "completed" | "unavailable";
            /** Summary */
            summary?: string | null;
            /** Detail */
            detail?: string | null;
            /** Eventtime */
            eventTime?: string | number | null;
            /** Evidence */
            evidence: {
                [key: string]: unknown;
            };
            /** Environment */
            environment: string;
        };
        /** FeeOverride */
        FeeOverride: {
            /**
             * Mode
             * @default per_lot
             * @enum {string}
             */
            mode: "per_lot" | "notional";
            /**
             * Open
             * @default 0
             */
            open: number;
            /**
             * Closetoday
             * @default 0
             */
            closeToday: number;
            /**
             * Closeyesterday
             * @default 0
             */
            closeYesterday: number;
            /**
             * Openperlotextra
             * @default 0
             */
            openPerLotExtra: number;
            /**
             * Closetodayperlotextra
             * @default 0
             */
            closeTodayPerLotExtra: number;
            /**
             * Closeyesterdayperlotextra
             * @default 0
             */
            closeYesterdayPerLotExtra: number;
            /** Symbol */
            symbol: string;
        };
        /** FeeRate */
        FeeRate: {
            /**
             * Mode
             * @default per_lot
             * @enum {string}
             */
            mode: "per_lot" | "notional";
            /**
             * Open
             * @default 0
             */
            open: number;
            /**
             * Closetoday
             * @default 0
             */
            closeToday: number;
            /**
             * Closeyesterday
             * @default 0
             */
            closeYesterday: number;
            /**
             * Openperlotextra
             * @default 0
             */
            openPerLotExtra: number;
            /**
             * Closetodayperlotextra
             * @default 0
             */
            closeTodayPerLotExtra: number;
            /**
             * Closeyesterdayperlotextra
             * @default 0
             */
            closeYesterdayPerLotExtra: number;
        };
        /** FeeSchedule */
        FeeSchedule: {
            /**
             * Source
             * @default account
             * @enum {string}
             */
            source: "account" | "custom";
            default?: components["schemas"]["FeeRate"];
            /** Overrides */
            overrides?: components["schemas"]["FeeOverride"][];
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /**
         * HealthAdvice
         * @description One 策略体检 suggestion from a rule backtest result (`advice[]`), sent back as it came. The change
         *     is decided by kind, groups and index; `text` becomes the new version's change note; extra fields
         *     (label, passes) are ignored.
         */
        HealthAdvice: {
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "drop" | "relaxCross";
            /** Groups */
            groups: ("entryLong" | "entryShort")[];
            /** Index */
            index?: number | null;
            /** Withinbars */
            withinBars?: number | null;
            /** Text */
            text?: string | null;
        };
        /**
         * HelpChange
         * @description 使用说明卡 (2026-09-25 两层重做): the card opens first when it is closed. One command may combine
         *     keys, applied in order: topic / group → query → detail → faq.
         */
        HelpChange: {
            /**
             * Topic
             * @description 打开该主题（旧 id 按别名解析）
             */
            topic?: string | null;
            /**
             * Group
             * @description 打开该分组的第一个主题；developer 同时展开导航底部的“开发者参考”
             */
            group?: string | null;
            /**
             * Query
             * @description 搜索词（匹配标题、summary、steps、faq、detail；空串清除；命中 detail 时自动展开完整说明）
             */
            query?: string | null;
            /**
             * Detail
             * @description 展开 / 收起当前主题的“完整说明”
             */
            detail?: boolean | null;
            /** @description 展开 / 收起当前主题的一个常见问题 */
            faq?: components["schemas"]["HelpFaqChange"] | null;
            /**
             * Retry
             * @description 说明读取失败后重新读取（卡上的“重试”；快照 state.help.canRetry 为 true 时可用，已读到时 rejected；卡没开时打开并读取）
             */
            retry?: true | null;
        };
        /** HelpFaq */
        HelpFaq: {
            /** Q */
            q: string;
            /** A */
            a: string;
        };
        /** HelpFaqChange */
        HelpFaqChange: {
            /**
             * Index
             * @description 常见问题的序号（从 0 数）
             */
            index: number;
            /** Open */
            open: boolean;
        };
        /** HelpGroup */
        HelpGroup: {
            /** Id */
            id: string;
            /** Title */
            title: string;
            /**
             * Owneronly
             * @default false
             */
            ownerOnly: boolean;
            /**
             * Collapsed
             * @default false
             */
            collapsed: boolean;
        };
        /** HelpSnapshot */
        HelpSnapshot: {
            /** Groups */
            groups: components["schemas"]["HelpGroup"][];
            /** Aliases */
            aliases: {
                [key: string]: string;
            };
            /** Topics */
            topics: {
                [key: string]: components["schemas"]["HelpTopic"];
            };
        };
        /** HelpTerm */
        HelpTerm: {
            /** Term */
            term: string;
            /** Plain */
            plain: string;
            /** When */
            when: string;
        };
        /**
         * HelpTopic
         * @description Fields in reading order: the plain layer (summary, steps, faq, glossary terms), then detail.
         */
        HelpTopic: {
            /** Title */
            title: string;
            /** Group */
            group: string;
            /** Summary */
            summary: string;
            /** Steps */
            steps: string[];
            /** Faq */
            faq: components["schemas"]["HelpFaq"][];
            /** Terms */
            terms?: components["schemas"]["HelpTerm"][] | null;
            /** Detail */
            detail: string[];
            /** Paragraphs */
            paragraphs: string[];
            /** Tour */
            tour?: components["schemas"]["TourStep"][] | null;
        };
        /**
         * HighlightChange
         * @description Pulse or outline a surface with a small label bubble; `clear: true` removes every highlight.
         */
        HighlightChange: {
            /**
             * Target
             * @description 表面/控件稳定 id：data-agent-target、data-testid 或 data-region 的值（快照 context.highlightTargets 列出可用值）
             */
            target?: string | null;
            /** Label */
            label?: string | null;
            /**
             * Style
             * @default pulse
             * @enum {string}
             */
            style: "pulse" | "outline";
            /**
             * Durationms
             * @description 1000–15000；0 = 清除该目标
             * @default 6000
             */
            durationMs: number;
            /**
             * Clear
             * @default false
             */
            clear: boolean;
        };
        /** InstrumentScope */
        InstrumentScope: {
            /**
             * Mode
             * @default any
             * @enum {string}
             */
            mode: "any" | "selected";
            /** Symbols */
            symbols?: string[];
        };
        /**
         * LiveTradingState
         * @description 设置 → 账户 → 实盘 (2026-10-08), while that page shows: always off, and why it cannot be turned on.
         */
        LiveTradingState: {
            /**
             * State
             * @constant
             */
            state: "off";
            /** Disabledreason */
            disabledReason: string;
        };
        /**
         * MemoryPanelState
         * @description 设置 → AI 与记忆 (desktop page or the phone's 记忆 page), while it shows (AI 审计 2026-10-13): whether
         *     memory is on and how many entries each layer holds; the entries themselves read GET /api/memory.
         */
        MemoryPanelState: {
            /** Enabled */
            enabled: boolean;
            /** Counts */
            counts: {
                [key: string]: number;
            };
        };
        /**
         * MobileChartCellChange
         * @description 手机看盘 · 多图 (2026-10-09): one cell of the current layout — its contract (null = 自动补位) and/or
         *     period. In 同合约 4 周期 the contract is the chart's own (a symbol there switches the current contract).
         */
        MobileChartCellChange: {
            /** Index */
            index: number;
            /** Symbol */
            symbol?: string | null;
            /** Duration */
            duration?: (60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800) | null;
        };
        /**
         * MobileLabChange
         * @description 手机版回测结果页 (2026-10-06): the comparison shown (`GET /api/strategy-labs/{labId}`).
         */
        MobileLabChange: {
            /** Labid */
            labId: string;
        };
        /**
         * MobileLabState
         * @description The 回测结果页's comparison and the variant 选用 of it (null until one is chosen).
         */
        MobileLabState: {
            /** Labid */
            labId: string;
            /** Chosen */
            chosen?: string | null;
        };
        /**
         * MobileMarketChange
         * @description 手机看盘 (2026-10-09), phone layout only; one key per command, each opens the 看盘 tab first.
         *     view 报价 / 多图 / 单图; board and columns land on 报价, layout and cell on 多图, subPane and subPanes
         *     (the 副图设置 sheet) on 单图. The values are account preferences (ui.mobileMarket, chart.subPaneList)
         *     reported in state.preferences; the sheet is disclosures.mobile-subpanes.
         */
        MobileMarketChange: {
            /** View */
            view?: ("quotes" | "multi" | "single") | null;
            /** Board */
            board?: ("watchlist" | "main" | "SHFE" | "DCE" | "CZCE" | "CFFEX" | "GFEX" | "INE" | "FOREIGN") | null;
            /** Columns */
            columns?: ("last" | "changePercent" | "change" | "openInterest" | "bid" | "ask" | "volume" | "openInterestChange")[] | null;
            /** Layout */
            layout?: ("four" | "periods" | "six") | null;
            cell?: components["schemas"]["MobileChartCellChange"] | null;
            /** Subpane */
            subPane?: ("kd" | "macd" | "rsi" | "oi" | "cci" | "volume") | null;
            /** Subpanes */
            subPanes?: boolean | null;
        };
        /** MonitorCardChange */
        MonitorCardChange: {
            /**
             * Name
             * @constant
             */
            name: "research";
            /** Expanded */
            expanded: boolean;
        };
        /** MonitorState */
        MonitorState: {
            /** Pausereason */
            pauseReason?: string | null;
            /** Consecutivelosses */
            consecutiveLosses: number;
        } & {
            [key: string]: unknown;
        };
        /** NewsNavigation */
        NewsNavigation: {
            /** Tab */
            tab?: ("sheet" | "desk" | "debate" | "flash" | "news" | "data") | null;
            /** Flashfilter */
            flashFilter?: ("product" | "watch" | "macro" | "all") | null;
            /** Flashsort */
            flashSort?: ("time" | "impact") | null;
            /** Pinnedsymbol */
            pinnedSymbol?: string | null;
            /** Deskrejudge */
            deskRejudge?: true | null;
            /** Desksource */
            deskSource?: string | null;
        };
        /** Offset */
        Offset: {
            /** Factor */
            factor: number;
            /**
             * Indicator
             * @enum {string}
             */
            indicator: "atr" | "range_nday";
            /**
             * Period
             * @default 14
             */
            period: number;
        };
        /** Operand */
        Operand: {
            /**
             * Indicator
             * @enum {string}
             */
            indicator: "constant" | "close" | "ma" | "macd_dif" | "macd_dea" | "macd_hist" | "kd_k" | "kd_d" | "volume" | "volume_ratio" | "ema" | "atr" | "donchian_high" | "donchian_low" | "boll_mid" | "boll_up" | "boll_down" | "keltner_mid" | "keltner_up" | "keltner_down" | "rsi" | "supertrend" | "prev_high" | "prev_low" | "prev_close" | "today_open" | "range_nday" | "gap_percent" | "opening_range_high" | "opening_range_low" | "cci" | "adx" | "plus_di" | "minus_di" | "sar" | "session_high" | "session_low" | "pivot" | "oi_change" | "oi_quadrant" | "roc" | "kd_j" | "trix" | "high" | "low" | "open" | "williams_r" | "bias" | "mfi" | "bbi" | "cmi" | "bar_range" | "body" | "upper_shadow" | "lower_shadow" | "vwap";
            /**
             * Timeframe
             * @default 0
             * @enum {integer}
             */
            timeframe: 0 | 60 | 180 | 300 | 900 | 3600 | 86400;
            /**
             * Period
             * @default 5
             */
            period: number;
            /**
             * Value
             * @default 0
             */
            value: number;
            /** K */
            k?: number | null;
            /** Basis */
            basis?: ("ema" | "ma") | null;
            /** Average */
            average?: number | null;
            /**
             * Barsago
             * @default 0
             */
            barsAgo: number;
            /** Highest */
            highest?: number | null;
            /** Lowest */
            lowest?: number | null;
            offset?: components["schemas"]["Offset"] | null;
        };
        /** OrderRequest */
        OrderRequest: {
            /** Symbol */
            symbol: string;
            /**
             * Direction
             * @enum {string}
             */
            direction: "BUY" | "SELL";
            /**
             * Offset
             * @enum {string}
             */
            offset: "OPEN" | "CLOSE" | "CLOSETODAY";
            /** Volume */
            volume: number;
            /**
             * Pricetype
             * @constant
             */
            priceType: "LIMIT";
            /** Limitprice */
            limitPrice: number;
        } & {
            [key: string]: unknown;
        };
        /** PinnedSymbolsChange */
        PinnedSymbolsChange: {
            /** Add */
            add?: string | null;
            /** Remove */
            remove?: string | null;
        };
        /**
         * PlanBacktestRequest
         * @description 不保存也能回测：带完整策略定义（给 AI 比较改前 / 改后，策略编辑卡的实时回测）。
         */
        PlanBacktestRequest: {
            /**
             * Symbol
             * @description 回测合约；省略时用策略绑定的合约，策略库版本须指定
             */
            symbol?: string | null;
            /**
             * Timeframe
             * @description 跟随图表周期的策略用哪个周期回测
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /**
             * Tradingdays
             * @description 只回测最近 N 个交易日；超过数据上限时用全部可取到的历史
             */
            tradingDays?: number | null;
            /**
             * Slippageticks
             * @description 开平各算几跳滑点，默认 0
             * @default 0
             */
            slippageTicks: number;
            /**
             * Initialbalance
             * @description 初始资金；省略时取账户权益，读不到用 100 万
             */
            initialBalance?: number | null;
            /**
             * Sessionid
             * @description 把结果卡发到这个对话
             */
            sessionId?: string | null;
            /**
             * Requestid
             * @description 调用方自取的请求号；排队时用 GET /api/strategy-backtests/queue?requestId= 查看位置
             */
            requestId?: string | null;
            /**
             * Fallbackreason
             * @description 界面本机优先时为什么改在服务端算（排障用，随结果存下）；AI 与 CLI 不用传
             */
            fallbackReason?: ("unsupported" | "device" | "warming" | "init_failed" | "init_timeout" | "memory" | "crashed" | "engine_error" | "hidden" | "slow" | "inputs_failed" | "inputs_expired" | "rerun") | null;
            plan: components["schemas"]["StrategyPlan"];
            /**
             * Strategyid
             * @description 对比的来源策略（只作标识）
             */
            strategyId?: string | null;
            /**
             * Preview
             * @description 策略编辑卡的实时回测：只算不存（不进最近回测、不写 AI 怎么看），结果多带一句 planSummary
             * @default false
             */
            preview: boolean;
        };
        /** PositionSelection */
        PositionSelection: {
            /** Symbol */
            symbol: string;
            /**
             * Direction
             * @enum {string}
             */
            direction: "long" | "short";
        };
        /**
         * PreferenceChange
         * @description Set one account preference from the settings UI: the client writes it through PUT /api/preferences
         *     and reports the document back in `preferences`. Agents normally use update_preferences directly.
         *     `ui.quickSkills` is the one list-valued key (the quick-skill bar's pinned skill ids).
         */
        PreferenceChange: {
            /**
             * Key
             * @description 点分路径，如 voice.speed、trade.defaultVolume、notify.desktop.fills、ui.quickSkills
             */
            key: string;
            /** Value */
            value?: string | number | boolean | null | string[];
        };
        /** PreferencesSnapshot */
        PreferencesSnapshot: {
            /** Revision */
            revision: number;
            preferences: components["schemas"]["PresentationPreferences"] | null;
        };
        /** PreferencesUpdate */
        PreferencesUpdate: {
            /** Revision */
            revision: number;
            preferences: components["schemas"]["PresentationPreferences"];
        };
        /** PresentationChanges */
        PresentationChanges: {
            /** Theme */
            theme?: ("dark" | "light" | "system") | null;
            /** Fontscale */
            fontScale?: ("standard" | "large" | "extra-large") | null;
            /** Density */
            density?: ("compact" | "standard" | "comfortable") | null;
            /** Dockside */
            dockSide?: ("left" | "right") | null;
            /** Dockwidth */
            dockWidth?: number | null;
            chartSizes?: components["schemas"]["ChartSizes"] | null;
            /** Reset */
            reset?: boolean | null;
        };
        /** PresentationPreferences */
        PresentationPreferences: {
            /**
             * Theme
             * @default dark
             * @enum {string}
             */
            theme: "dark" | "light" | "system";
            /**
             * Fontscale
             * @default large
             * @enum {string}
             */
            fontScale: "standard" | "large" | "extra-large";
            /**
             * Density
             * @default standard
             * @enum {string}
             */
            density: "compact" | "standard" | "comfortable";
            /**
             * Dockside
             * @default right
             * @enum {string}
             */
            dockSide: "left" | "right";
            chart: components["schemas"]["ChartOptions"];
            chartSizes: components["schemas"]["ChartSizes"];
            /**
             * Positionsbar
             * @default collapsed
             * @enum {string}
             */
            positionsBar: "expanded" | "collapsed";
            /** Recentsymbols */
            recentSymbols?: string[];
            /** Pinnedsymbols */
            pinnedSymbols?: string[];
            /** Initialbalance */
            initialBalance?: number | null;
        };
        /**
         * PullbackSetup
         * @description A bounded, single-clock trend pullback; levels freeze on the signal bar.
         */
        PullbackSetup: {
            /**
             * Trendperiod
             * @default 200
             */
            trendPeriod: number;
            /**
             * Touchperiod
             * @default 25
             */
            touchPeriod: number;
            /**
             * Guardperiod
             * @default 60
             */
            guardPeriod: number;
            /**
             * Maxbars
             * @default 10
             */
            maxBars: number;
        };
        /** PushDevice */
        PushDevice: {
            /** Deviceid */
            deviceId: string;
            /** Platform */
            platform: string;
            /** Createdat */
            createdAt: string;
            /** Lastokat */
            lastOkAt?: string | null;
            /** Lasterror */
            lastError?: string | null;
        };
        /** PushKeys */
        PushKeys: {
            /** P256Dh */
            p256dh: string;
            /** Auth */
            auth: string;
        };
        /** PushStatus */
        PushStatus: {
            /**
             * Configured
             * @description 服务端是否已配置推送密钥；false 时不能开启
             */
            configured: boolean;
            /**
             * Publickey
             * @description VAPID 公钥（applicationServerKey，base64url）
             */
            publicKey?: string | null;
            /** Devices */
            devices: components["schemas"]["PushDevice"][];
        };
        /** PushSubscribe */
        PushSubscribe: {
            /**
             * Deviceid
             * @description 本设备 id（与界面心跳的 device.deviceId 相同）
             */
            deviceId: string;
            /**
             * Platform
             * @description 设备名，如“iPhone · 桌面图标”“Mac · Safari”
             */
            platform: string;
            subscription: components["schemas"]["PushSubscriptionBody"];
        };
        /**
         * PushSubscriptionBody
         * @description PushSubscription.toJSON() as the browser gives it.
         */
        PushSubscriptionBody: {
            /** Endpoint */
            endpoint: string;
            keys: components["schemas"]["PushKeys"];
            /** Expirationtime */
            expirationTime?: number | null;
        };
        /** PushTest */
        PushTest: {
            /**
             * Deviceid
             * @description 只发给这台；省略发给本人所有设备
             */
            deviceId?: string | null;
        };
        /** PushTestResult */
        PushTestResult: {
            /** Deviceid */
            deviceId: string;
            /** Platform */
            platform: string;
            /**
             * Status
             * @enum {string}
             */
            status: "sent" | "removed" | "failed";
            /** Detail */
            detail?: string | null;
        };
        /** PushTestResults */
        PushTestResults: {
            /** Results */
            results: components["schemas"]["PushTestResult"][];
        };
        /**
         * Pyramiding
         * @description 加仓：浮盈方向顺势加（不摊平）。trigger 决定何时加：profit_atr / profit_ticks 按上次成交价
         *     与决策K线收盘的距离，signal 按同向入场组再成立且收盘更优；step 是距离（ATR 倍数或跳数）。
         *     volume 是每次加仓手数（缺省用首次开仓手数）；moveStop 为加仓后按最近一次成交重算止损/止盈。
         */
        Pyramiding: {
            /** Maxadds */
            maxAdds: number;
            /**
             * Trigger
             * @default profit_atr
             * @enum {string}
             */
            trigger: "profit_atr" | "profit_ticks" | "signal";
            /**
             * Step
             * @default 0.5
             */
            step: number;
            /**
             * Atrperiod
             * @default 20
             */
            atrPeriod: number;
            /** Volume */
            volume?: number | null;
            /**
             * Movestop
             * @default true
             */
            moveStop: boolean;
        };
        /**
         * ReminderCenterChange
         * @description 提醒中心: open or close it (the computer's bell popover, the phone's 提醒 page) and pick its filter;
         *     a filter alone also opens it. `more` = 继续加载当天提醒 (#932 R5): the next batch of a day past its first
         *     500 reminders, while the snapshot's moreToday says there is one (opens the center too).
         */
        ReminderCenterChange: {
            /** Open */
            open?: boolean | null;
            /** Filter */
            filter?: ("unread" | "all") | null;
            /** More */
            more?: true | null;
        };
        /**
         * ReminderCenterState
         * @description 提醒中心, reported while the window has one (a member's connected window): open, its filter, the
         *     user's unread count, how many reminders of the current trading day are read so far, whether more of them
         *     are still to read past the page limit (moreToday: 继续加载当天提醒), the last `more` command's outcome (moreAsked =
         *     its number, moreDone = the number whose read finished, moreError = why it loaded nothing) and whether 查看更早
         *     was read.
         */
        ReminderCenterState: {
            /** Open */
            open: boolean;
            /**
             * Filter
             * @enum {string}
             */
            filter: "unread" | "all";
            /** Unread */
            unread: number;
            /** Today */
            today: number;
            /**
             * Moretoday
             * @default false
             */
            moreToday: boolean;
            /**
             * Moreasked
             * @default 0
             */
            moreAsked: number;
            /**
             * Moredone
             * @default 0
             */
            moreDone: number;
            /**
             * Moreerror
             * @default
             */
            moreError: string;
            /**
             * Earlier
             * @default false
             */
            earlier: boolean;
        };
        /**
         * ReminderOptions
         * @description 提醒页的提醒怎么响 (改版第一步 2026-10-31)，只用于 reminderOnly 的计划。
         */
        ReminderOptions: {
            /**
             * Repeat
             * @description 响几次：once 只响一次（响过就自动停）；rearm 同一个信号只响一次，条件不再满足后再满足才再响（原来的做法）；bar 条件满足的每根 K 线最多响一次。
             * @default rearm
             * @enum {string}
             */
            repeat: "once" | "rearm" | "bar";
            /**
             * Until
             * @description 到期：today 开始提醒那个交易日收盘、week 本周最后一个交易日收盘、forever 一直；到期自动停并说一声。
             * @default forever
             * @enum {string}
             */
            until: "today" | "week" | "forever";
            /**
             * Watchlist
             * @description 整张自选表：开始提醒时按自选表每个品种的主力合约各上线一个实例，每个品种分别判断（上线后自选表再变不跟）。
             * @default false
             */
            watchlist: boolean;
        };
        /**
         * RemindersNavigation
         * @description 提醒面板（电脑右侧栏）/ 提醒页（手机）的页签（2026-10-25）：mine 我的提醒（到价、画线、条件提醒一张表）/ general 通用提醒（原设置 → 提醒）/ history 提醒记录（原提醒中心）。
         */
        RemindersNavigation: {
            /** Tab */
            tab?: ("mine" | "general" | "history") | null;
        };
        /** RemoteSettingsChange */
        RemoteSettingsChange: {
            /** Enabled */
            enabled?: boolean | null;
            /** Bindings */
            bindings?: {
                [key: string]: "none" | "ptt" | "cancel" | "chat" | "settings" | "scrollUp" | "scrollDown" | "focusPrevious" | "focusNext" | "activate";
            } | null;
            /** Resetbindings */
            resetBindings?: boolean | null;
            /** Diagnostic */
            diagnostic?: boolean | null;
            /** Disconnect */
            disconnect?: boolean | null;
        };
        /**
         * ResearchDebateChange
         * @description 多 AI 研讨 (2026-10-11; its own page 2026-10-24): the 辩论 page — the right rail's 辩论 on a computer (half
         *     the window, `panel: debate`), a full-screen page over the 调研 tab on a phone. One field per command: `start` =
         *     press 研讨 on a 快讯 event (`start: <事件 id>`, events[].id), 让 AI 们辩一轮 on 今日判断 (`start: "desk"`) or the
         *     newest 重要 event of the product being read (`start: "event"`) — an event already debated today, or an
         *     unchanged 今日议题, opens its result instead of running again; `open` shows one debate by id; `rerun` is
         *     再辩一轮 on the open one (a new run); `close` closes the page. Starting runs POST /api/research/debates (four
         *     analysts' reports written at once, then four spoken turns one after another, then the judge — about two to three
         *     minutes, the day's cap applies; reports and turns stream into the page; a finished report's card folds with
         *     `disclosure: debate-analyst-<technical|supply|flow|news>`); 写进今日判断 is POST /api/research/debates/{id}/adopt.
         */
        ResearchDebateChange: {
            /**
             * Start
             * @description desk = 今日议题；event = 当前品种最新一条重要快讯；其余为快讯事件 id（events[].id）
             */
            start?: string | null;
            /**
             * Open
             * @description 打开这次研讨（GET /api/research/debates 的 id）
             */
            open?: string | null;
            /**
             * Rerun
             * @description 再辩一轮：对打开的这次研讨重新跑（新一次，计入当日次数）
             */
            rerun?: true | null;
            /**
             * Close
             * @description 返回：收起研讨子视图
             */
            close?: true | null;
        };
        /** ReviewExpandChange */
        ReviewExpandChange: {
            /** Month */
            month?: string | null;
            /** Day */
            day?: string | null;
        };
        /** ReviewUiState */
        ReviewUiState: {
            /** Month */
            month?: string | null;
            /** Day */
            day?: string | null;
            /**
             * Filter
             * @default
             */
            filter: string;
            /** Selectedtradeid */
            selectedTradeId?: string | null;
            /** Selectednote */
            selectedNote?: string | null;
            /** Tiles */
            tiles?: {
                [key: string]: unknown;
            }[] | null;
            /** Equitycurve */
            equityCurve?: {
                [key: string]: unknown;
            } | null;
            /** Unlinkednotes */
            unlinkedNotes?: number | null;
            /** Dailyreview */
            dailyReview?: {
                [key: string]: unknown;
            } | null;
            /** Habits */
            habits?: {
                [key: string]: unknown;
            } | null;
            /** Currency */
            currency?: ("CNY" | "USD") | null;
            /** Currencies */
            currencies?: ("CNY" | "USD")[] | null;
        };
        /** RoutineSummaryState */
        RoutineSummaryState: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Steps */
            steps?: string[];
            /** Lastrunat */
            lastRunAt?: string | null;
        };
        /**
         * RoutinesPanelState
         * @description 我的流程 (设置 → AI 与记忆), while it shows (AI 审计 2026-10-13): the saved routines in order, each
         *     with its steps as the list shows them; GET /api/routines carries the full definitions.
         */
        RoutinesPanelState: {
            /** Count */
            count: number;
            /** Items */
            items?: components["schemas"]["RoutineSummaryState"][];
        };
        /** RuleGroup */
        RuleGroup: {
            /**
             * Join
             * @default all
             * @enum {string}
             */
            join: "all" | "any";
            /**
             * Sequence
             * @default false
             */
            sequence: boolean;
            /** Conditions */
            conditions?: components["schemas"]["Condition"][];
            /**
             * Branches
             * @description 条件组合：每个分支先在组内按自己的join合并，再与conditions里的单条条件一起按本组join合并；例如 join=any、两个 all 分支 = (A且B)或(C且D)。
             */
            branches?: components["schemas"]["Branch"][];
            /**
             * Alignmentconditions
             * @description 入场方向状态，全部同向后等待conditions确认；方向被打断才开始新的一段，每段最多一次开仓尝试。
             */
            alignmentConditions?: components["schemas"]["Condition"][];
        };
        /** SavePlanRequest */
        SavePlanRequest: {
            plan: components["schemas"]["StrategyPlan"];
            instrumentScope?: components["schemas"]["InstrumentScope"];
            /**
             * Basestrategyid
             * @description 修改的是哪条策略（策略库版本的 strategyId；运行实例的 id 取它的策略库版本）：存为它的新版本，它定义里卡片以外的字段（模板来源等）原样带上
             */
            baseStrategyId?: string | null;
            /**
             * Templateid
             * @description 从哪个模板开始（新建时）：记为模板来源，和模板默认不同时标为已调整
             */
            templateId?: string | null;
        };
        /**
         * ScaleOutTier
         * @description 分批止盈的一档：K线收盘时对持仓均价的浮盈达 profitTicks 跳或 profitAtr×ATR14（入场时冻结在
         *     exit_atr），平掉峰值手数的 percent%；只剩 1 手时该档作废（仍记为已触发）。
         */
        ScaleOutTier: {
            /** Profitticks */
            profitTicks?: number | null;
            /** Profitatr */
            profitAtr?: number | null;
            /** Percent */
            percent: number;
        };
        /**
         * Sizing
         * @description atr_risk：手数 = floor(权益 × riskPercent% ÷ (合约乘数 × atrMultiple × ATR))，上限 maxPosition。
         *     equity_percent (2026-10-09)：本策略保证金预算 = 权益 × equityPercent%，先扣本策略持仓与未成交
         *     开仓挂单占用的保证金再除以每手保证金；可选 maxLots 限制本策略总手数（持仓+挂单），不填不限。
         */
        Sizing: {
            /**
             * Mode
             * @default fixed
             * @enum {string}
             */
            mode: "fixed" | "atr_risk" | "equity_percent";
            /** Riskpercent */
            riskPercent?: number | null;
            /**
             * Atrmultiple
             * @default 2
             */
            atrMultiple: number;
            /**
             * Atrperiod
             * @default 14
             */
            atrPeriod: number;
            /**
             * Equitypercent
             * @description 按资金比例定手数：本策略保证金占用上限占账户权益的百分比。
             */
            equityPercent?: number | null;
            /**
             * Maxlots
             * @description 按资金比例定手数时本策略总手数上限（持仓+未成交开仓挂单）；不填不限。
             */
            maxLots?: number | null;
        };
        /**
         * SpreadLeg
         * @description 价差的一条腿：一个具体合约，及这份价差里它占的手数（lots）与权重（weight，仅差价用）。
         */
        SpreadLeg: {
            /** Symbol */
            symbol: string;
            /**
             * Weight
             * @default 1
             */
            weight: number;
            /**
             * Lots
             * @default 1
             */
            lots: number;
        };
        /**
         * SpreadSpec
         * @description 价差 (2026-09-29 第一期): two legs of one account traded as one unit — 做多价差 = 买 A 卖 B,
         *     做空价差 = 卖 A 买 B, one unit = A lotsA 手 + B lotsB 手. The rules judge the spread series
         *     (``difference`` A×wA − B×wB, the weights; ``ratio`` A ÷ B, the weights ignored) on the two legs'
         *     same-period closed K-lines inner-joined by time. ``maxPosition`` counts spread units; the plan's
         *     stopLossTicks / takeProfitTicks do not apply (use stopLoss / takeProfit in spread points here).
         */
        SpreadSpec: {
            /**
             * Kind
             * @default difference
             * @enum {string}
             */
            kind: "difference" | "ratio";
            /** Legs */
            legs: components["schemas"]["SpreadLeg"][];
            /**
             * Stoploss
             * @description 价差止损点数：实盘每秒按两腿最新价判断，回测按收盘价差判断。
             */
            stopLoss?: number | null;
            /**
             * Takeprofit
             * @description 价差止盈点数：实盘每秒按两腿最新价判断，回测按收盘价差判断。
             */
            takeProfit?: number | null;
        };
        /**
         * StrategyBacktestChange
         * @description 规则回测 (2026-09-25) on the selected strategy's 详情: the parameter row, 重跑, 在图上标出买卖点
         *     and the folded 逐笔清单. run=true runs the same POST …/backtest the 回测 button sends.
         */
        StrategyBacktestChange: {
            /**
             * Tradingdays
             * @description 区间：最近 N 个交易日
             */
            tradingDays?: number | null;
            /**
             * Slippageticks
             * @description 滑点跳数
             */
            slippageTicks?: number | null;
            /**
             * Run
             * @description 按当前参数回测（与“回测”按钮同一接口）
             */
            run?: true | null;
            /**
             * Mark
             * @description true 把开平仓点画到图上（先切到回测的合约与周期），false 清除
             */
            mark?: boolean | null;
            /**
             * Trades
             * @description 展开 / 收起逐笔清单
             */
            trades?: boolean | null;
            /**
             * Applyadvice
             * @description 策略体检的“按这个改”：按回测结果里这条建议（advice id）改好，存为新版本并自动重跑回测
             */
            applyAdvice?: string | null;
        };
        /** StrategyDecision */
        StrategyDecision: {
            /** Status */
            status: string;
            /** Detail */
            detail?: string | null;
            /** Checkedat */
            checkedAt?: string | null;
            /** Since */
            since?: string | null;
            /** Checks */
            checks?: number | null;
            /** Waitreason */
            waitReason?: string | null;
            /** Waitingsince */
            waitingSince?: string | null;
            /** Stalled */
            stalled?: boolean | null;
        } & {
            [key: string]: unknown;
        };
        /** StrategyDetailsChange */
        StrategyDetailsChange: {
            /**
             * Section
             * @enum {string}
             */
            section: "rules" | "scope" | "executions" | "idea";
            /** Open */
            open: boolean;
        };
        /**
         * StrategyEditorChange
         * @description 策略编辑卡 (2026-10-08): 新建或修改策略都打开这张卡（策略面板里）。One command may combine keys, applied in
         *     order: close → open / new / edit → start → answers → ask → focus → save. The card only designs: saving stores a
         *     draft (a new version when editing), 上线运行 stays the user's click on the existing launch dialog.
         */
        StrategyEditorChange: {
            /**
             * Open
             * @description 打开编辑卡：有没存的草稿就接着改，没有就新建（同 new）
             */
            open?: true | null;
            /**
             * New
             * @description 新建：起点默认“按我的习惯”（近 20 个交易日已平仓成交至少 5 笔时），不够就空白；合约默认当前图表合约
             */
            new?: true | null;
            /**
             * Reminder
             * @description 和 new 一起用：新建提醒（提醒模式，2026-10-25 第 6 批）——只填条件和提醒时段，不填手数和止损；保存为只提醒的策略（plan.reminderOnly），列在提醒面板“我的提醒”、不出现在“我的策略”，“开始提醒”按只提醒上线（不出卡、不下单）
             */
            reminder?: true | null;
            /**
             * Edit
             * @description 修改我的策略里的这条（strategyId）：卡片改不了的规则原样保留，存的时候存为新版本，原版本保留
             */
            edit?: string | null;
            /**
             * Start
             * @description 起点（替换表单内容）：blank 空白 / habits 按我的习惯（成交不够时打开空白，快照 prefill 写明原因）/ template:<模板id> 从模板（模板 id 见 GET /api/strategy-templates）
             */
            start?: string | null;
            /**
             * Answers
             * @description 回答补一句后 AI 追问的问题（快照 questions）：问题 id → 回答，答完带着答案重新生成
             */
            answers?: {
                [key: string]: string;
            } | null;
            /**
             * Ask
             * @description 补一句：表单空时按这句生成整份计划，已有内容时只改相关字段（POST /api/strategy-plans/design-jobs，带 previousPlan）；改到的字段高亮，不直接保存
             */
            ask?: string | null;
            /**
             * Focus
             * @description 流程图定位：把表单滚到这一块并短暂高亮（同点流程图的方块）——四段 basics / entry / exit / sizing，或流程图方块 trigger 触发、entryLong / entryShort 开多 / 开空条件、action 开仓手数、stop 止损、take 止盈、trailing 追踪、breakeven 保本、scaleOut 分批止盈、timeTakeProfit 按时间止盈、maxHold 最长持仓、closeBeforeEnd 收盘前平仓、exitRules 按条件平仓、limits 冷却与每天次数；在“更多”里的会先展开；手机上展开对应的段（快照 state.strategyEditor.focus）
             */
            focus?: ("basics" | "entry" | "exit" | "sizing" | "trigger" | "entryLong" | "entryShort" | "action" | "stop" | "take" | "trailing" | "breakeven" | "scaleOut" | "timeTakeProfit" | "maxHold" | "closeBeforeEnd" | "exitRules" | "limits") | null;
            /**
             * Save
             * @description 存为草稿（开仓条件和止损填好才能存；修改已有策略时存为新版本），存好后打开它的结果页并自动回测
             */
            save?: true | null;
            /**
             * Close
             * @description 关闭编辑卡，放弃没存的改动
             */
            close?: true | null;
            /** @description 点选卡上的几项（不经 AI，改了要存）：提醒模式的 repeat / until / watchlist，策略的 signalAction；快照 state.strategyEditor.fields.reminder 与 fields.signalAction */
            choose?: components["schemas"]["StrategyEditorChoice"] | null;
        };
        /**
         * StrategyEditorChoice
         * @description 编辑卡上点选的几项 (改版第一步 2026-10-31)：提醒的响几次、到期、整张自选表；策略的“信号来了怎么办”。
         */
        StrategyEditorChoice: {
            /**
             * Repeat
             * @description 提醒·响几次：once 只响一次（响过自动停）/ rearm 条件再满足再响 / bar 每根 K 线最多一次
             */
            repeat?: ("once" | "rearm" | "bar") | null;
            /**
             * Until
             * @description 提醒·到期：today 今天收盘 / week 本周 / forever 一直（到期自动停并说一声）
             */
            until?: ("today" | "week" | "forever") | null;
            /**
             * Watchlist
             * @description 提醒·盯哪个品种：true 整张自选表（开始提醒时按每个自选品种的主力合约各上线一个，分别判断）/ false 只盯卡上的合约
             */
            watchlist?: boolean | null;
            /**
             * Signalaction
             * @description 策略·信号来了怎么办：上线运行对话框默认选中的执行方式（alert 只提醒 / confirm 出卡确认 / auto 自动下单，auto 要 设置 → 交易 → AI 自主执行 开着）；上线仍由用户点
             */
            signalAction?: ("alert" | "confirm" | "auto") | null;
        };
        /**
         * StrategyInstanceChange
         * @description Retired (#932 复核): a running card's 执行方式 is a trading control. The field stays in the schema for older
         *     agents and is refused with the interface that changes it (SIGNAL_ACTION_REFUSED).
         */
        StrategyInstanceChange: {
            /** Strategyid */
            strategyId: string;
            /**
             * Signalaction
             * @enum {string}
             */
            signalAction: "alert" | "confirm" | "auto";
        };
        /** StrategyItem */
        StrategyItem: {
            /** Strategyid */
            strategyId: string;
            /** Name */
            name: string;
            /** Version */
            version: number;
        };
        /**
         * StrategyLabChange
         * @description 试几组参数比一比 (策略页改版第八批, 2026-10-08; 原“实验室”页签已并入结果页): 选中策略最关键的一个
         *     参数跑当前值和上下各一组，前 70% 历史用来挑、后 30% 单独检验；界面默认在本机浏览器算（本机优先，
         *     2026-10-12），算不了自动转云端。open 打开选中策略的结果页并停在比较；run 开始比较；labId 打开那次
         *     比较所属的策略（旧比较没有所属策略时只回报那次比较）；choose 把某组存为我的策略（服务端先重跑权威
         *     回测，name 可选名字）。useServer 是旧字段，接受但不再区分。
         */
        StrategyLabChange: {
            /**
             * Open
             * @description 打开选中策略的结果页，停在“试几组参数比一比”
             */
            open?: boolean | null;
            /**
             * Labid
             * @description 打开这次比较所属策略的结果页
             */
            labId?: string | null;
            /**
             * Run
             * @description 对选中策略开始比较（POST /api/strategy-plans/{id}/compare）
             */
            run?: true | null;
            /**
             * Useserver
             * @description 旧字段：接受但不再区分（界面默认本机算，算不了转云端）
             */
            useServer?: boolean | null;
            /**
             * Choose
             * @description 把这组变体存为我的策略（variantId，服务端先重跑权威回测）
             */
            choose?: string | null;
            /**
             * Name
             * @description 存为我的策略时的名字（配合 choose）
             */
            name?: string | null;
        };
        /**
         * StrategyLaunchChange
         * @description The open 上线 dialog's fields (open it with `dialog: strategy-launch`); submitting stays a click.
         */
        StrategyLaunchChange: {
            /** Symbol */
            symbol?: string | null;
            /** Signalaction */
            signalAction?: ("alert" | "confirm" | "auto") | null;
        };
        /** StrategyPlan */
        StrategyPlan: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /**
             * Direction
             * @default both
             * @enum {string}
             */
            direction: "long" | "short" | "both";
            /**
             * Timeframeseconds
             * @description 0 表示按条件所用周期动态解析；用户明确固定执行周期时填写秒数。
             * @default 0
             * @enum {integer}
             */
            timeframeSeconds: 0 | 60 | 180 | 300 | 900 | 3600 | 86400;
            /**
             * Maxposition
             * @default 1
             */
            maxPosition: number;
            /**
             * Maxloss
             * @description 当日净亏损上限（元），含已实现、持仓盈亏及手续费；触及后仅禁止新开仓，退出条件和保护仍运行。null=不设：只有用户明确说了当日亏损上限才填（2026-10-08 用户决定）。
             */
            maxLoss?: number | null;
            /**
             * Maxdrawdownpercent
             * @description 回撤停机 (2026-10-09)：本策略净值从本轮最高点回撤达到该百分比时，撤销本策略未成交开仓单、平掉本策略持仓并保持停止，手动恢复。null=不启用；只按本策略归属的净值计算。
             */
            maxDrawdownPercent?: number | null;
            /**
             * Stoplossticks
             * @default 20
             */
            stopLossTicks: number;
            /**
             * Takeprofitticks
             * @default 40
             */
            takeProfitTicks: number | null;
            pullback?: components["schemas"]["PullbackSetup"] | null;
            /**
             * Stoplookbackbars
             * @description 入场时取最近N根已收盘K线（含信号K线）最低/最高价外一跳作为固定止损；与pullback互斥。
             */
            stopLookbackBars?: number | null;
            /** Pertraderisk */
            perTradeRisk?: number | null;
            /**
             * Cooldownbars
             * @description 平仓后等待的执行周期K线根数；退出不受冷却限制。平仓确认后才允许下一根信号开仓，不能同次反手。
             * @default 1
             */
            cooldownBars: number;
            /**
             * Maxordersperday
             * @default 20
             */
            maxOrdersPerDay: number;
            /** @description ATR 止损；与 stopLookbackBars、pullback 只能选一种，设置后不用 stopLossTicks。 */
            atrStop?: components["schemas"]["AtrStop"] | null;
            trailingStop?: components["schemas"]["TrailingStop"] | null;
            /**
             * Closebeforeendminutes
             * @description 收盘前 N 分钟平掉持仓；同一交易日同一持仓只触发一次。
             */
            closeBeforeEndMinutes?: number | null;
            /**
             * Closebeforeendscope
             * @description day 只在当日最后一个时段收盘前；session 每个交易时段收盘前都平。
             * @default day
             * @enum {string}
             */
            closeBeforeEndScope: "day" | "session";
            /**
             * Maxholdbars
             * @description 持仓达到 N 根执行周期K线即平仓。
             */
            maxHoldBars?: number | null;
            sizing?: components["schemas"]["Sizing"] | null;
            /**
             * Entrywindows
             * @description 只在这些时段开仓（按信号K线收盘时刻）；空表示不限。
             */
            entryWindows?: components["schemas"]["EntryWindow"][];
            /**
             * Allownightentries
             * @description false 时夜盘不开新仓，出场照常。
             * @default true
             */
            allowNightEntries: boolean;
            /**
             * Maxentriesperdirection
             * @description 每个交易日每个方向最多开仓次数。
             */
            maxEntriesPerDirection?: number | null;
            breakeven?: components["schemas"]["Breakeven"] | null;
            /**
             * Timetakeprofit
             * @description 分段止盈：收盘时取 afterBars 不超过已持仓根数的最大一档，浮盈达标即平；每笔持仓一次。
             */
            timeTakeProfit?: components["schemas"]["TimeTarget"][];
            /** @description 加仓：浮盈方向顺势加（不摊平），总手数不超过 maxPosition；分批止盈触发后不再加仓。 */
            pyramiding?: components["schemas"]["Pyramiding"] | null;
            /**
             * Scaleout
             * @description 分批止盈：最多 3 档、目标由近到远，对持仓均价判断；每档平掉峰值手数的 percent%，每笔持仓每档一次。
             */
            scaleOut?: components["schemas"]["ScaleOutTier"][];
            /** @description 价差：两条腿按一个整体交易（做多价差 = 买 A 卖 B）；规则判定两腿收盘价合成的价差序列。maxPosition 为价差份数，stopLossTicks/takeProfitTicks 不生效。 */
            spread?: components["schemas"]["SpreadSpec"] | null;
            rules: components["schemas"]["StrategyRules"];
            /**
             * Reminderonly
             * @description 只提醒 (2026-10-25 第 6 批)：提醒面板“新建提醒”存的策略——只填条件和时段，手数止损不用；上线只能“信号来了只提醒”，不出卡、不下单，列在“我的提醒”、不出现在“我的策略”。
             * @default false
             */
            reminderOnly: boolean;
            /** @description 提醒页的提醒的响几次、到期与自选表（只用于 reminderOnly）；null＝原来的做法（条件再满足才再响、一直提醒）。 */
            reminder?: components["schemas"]["ReminderOptions"] | null;
            /**
             * Signalaction
             * @description 信号来了怎么办：上线运行对话框默认选中的执行方式（只是默认，上线仍由用户点；自动下单仍要 AI 自主执行开着）。null＝出卡确认。
             */
            signalAction?: ("alert" | "confirm" | "auto") | null;
            /** Questions */
            questions?: string[];
            /** Clarifications */
            clarifications?: components["schemas"]["Clarification"][];
        };
        /** StrategyRules */
        StrategyRules: {
            entryLong?: components["schemas"]["EntryGroup"] | null;
            exitLong?: components["schemas"]["RuleGroup"] | null;
            entryShort?: components["schemas"]["EntryGroup"] | null;
            exitShort?: components["schemas"]["RuleGroup"] | null;
        };
        /**
         * StrategyTemplateChange
         * @description 策略模板库 on the 结果页: restore 让来自模板的策略恢复模板默认参数（另存为新版本）。
         *     The gallery, the parameter page and the template chips were retired 2026-10-08: a template is now the
         *     策略编辑卡's 起点 (`strategyEditor: {new: true, start: "template:<id>"}`). Their keys stay in the schema
         *     for older agents and are refused with that pointer.
         */
        StrategyTemplateChange: {
            /**
             * Open
             * @description 已下线：模板画廊并入策略编辑卡的“从模板”
             */
            open?: boolean | null;
            /**
             * Templateid
             * @description 已下线：用 strategyEditor {new: true, start: template:<id>}
             */
            templateId?: string | null;
            /**
             * Params
             * @description 已下线
             */
            params?: {
                [key: string]: number | null;
            } | null;
            /**
             * Timeframe
             * @description 已下线
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /**
             * Run
             * @description 已下线
             */
            run?: true | null;
            /**
             * Save
             * @description 已下线
             */
            save?: true | null;
            /**
             * Restore
             * @description 结果页：来自模板的策略恢复模板默认参数（另存新版本）
             */
            restore?: true | null;
        };
        /** StrategyUiChange */
        StrategyUiChange: {
            /** Selectedstrategyid */
            selectedStrategyId?: string | null;
            /** Tab */
            tab?: ("library" | "fees") | null;
            /** Designeropen */
            designerOpen?: boolean | null;
        };
        /** StrategyUiState */
        StrategyUiState: {
            /** Selectedstrategyid */
            selectedStrategyId: string;
            /**
             * Tab
             * @enum {string}
             */
            tab: "library" | "fees";
            /** Designeropen */
            designerOpen: boolean;
            /** Items */
            items: components["schemas"]["StrategyItem"][];
        };
        /**
         * TemplateInstantiateRequest
         * @description 从模板创建一条策略库草稿（与自己设计的策略完全等价）。
         */
        TemplateInstantiateRequest: {
            /**
             * Params
             * @description 要改的参数；省略的取默认值
             */
            params?: {
                [key: string]: number | string | null;
            };
            /**
             * Symbol
             * @description 只用于这个品种或合约（适用范围），例如 SHFE.rb、CME.GCE；外盘图表的快期外盘主连（KQD.m@COMEX.GC）按该外盘品种的全部月份；省略为全部品种
             */
            symbol?: string | null;
            /**
             * Name
             * @description 策略名称；省略为“模板名 · 合约中文名 · 周期”。与已有策略重名时追加“(2)”等序号
             */
            name?: string | null;
            /**
             * Chartsymbol
             * @description 默认名里用的合约（通常是当前图表合约）；只用于命名，不限制适用范围
             */
            chartSymbol?: string | null;
            /**
             * Timeframe
             * @description 执行周期；省略用模板推荐周期
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /** @description 完整适用范围（给了就不看 symbol），恢复模板默认时沿用原策略的范围 */
            instrumentScope?: components["schemas"]["InstrumentScope"] | null;
            /**
             * Basestrategyid
             * @description 恢复模板默认：存成这条策略（同名）的新版本，不另起名字
             */
            baseStrategyId?: string | null;
        };
        /**
         * TemplatePreviewRequest
         * @description 模板预览：按参数实例化出的策略定义，外加一次规则回测（口径同 POST /api/strategy-backtests）。
         */
        TemplatePreviewRequest: {
            /**
             * Symbol
             * @description 回测合约，例如 SHFE.rb2610
             */
            symbol: string;
            /**
             * Timeframe
             * @description 执行周期；省略用模板推荐周期
             */
            timeframe?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
            /**
             * Tradingdays
             * @description 只回测最近 N 个交易日；超过数据上限时用全部可取到的历史
             */
            tradingDays?: number | null;
            /**
             * Slippageticks
             * @description 开平各算几跳滑点，默认 0
             * @default 0
             */
            slippageTicks: number;
            /**
             * Initialbalance
             * @description 初始资金；省略时取账户权益，读不到用 100 万
             */
            initialBalance?: number | null;
            /**
             * Sessionid
             * @description 把结果卡发到这个对话
             */
            sessionId?: string | null;
            /**
             * Requestid
             * @description 调用方自取的请求号；排队时用 GET /api/strategy-backtests/queue?requestId= 查看位置
             */
            requestId?: string | null;
            /**
             * Fallbackreason
             * @description 界面本机优先时为什么改在服务端算（排障用，随结果存下）；AI 与 CLI 不用传
             */
            fallbackReason?: ("unsupported" | "device" | "warming" | "init_failed" | "init_timeout" | "memory" | "crashed" | "engine_error" | "hidden" | "slow" | "inputs_failed" | "inputs_expired" | "rerun") | null;
            /**
             * Params
             * @description 要改的参数（键见模板的 params）；省略的取默认值
             */
            params?: {
                [key: string]: number | string | null;
            };
        };
        /**
         * TimeTarget
         * @description 分段止盈的一档：持仓满 afterBars 根起，收盘浮盈达 profitTicks 跳或 profitAtr×ATR14 即平仓。
         */
        TimeTarget: {
            /** Afterbars */
            afterBars: number;
            /** Profitticks */
            profitTicks?: number | null;
            /** Profitatr */
            profitAtr?: number | null;
        };
        /**
         * TourChange
         * @description A guided tour: `start` with 1–20 steps, then `next` / `prev` / `stop`. Each step opens its surface,
         *     highlights its target and shows the step indicator (第 n/N 步 · 上一步 / 下一步 / 结束). A click anywhere
         *     only closes the bubble; only `stop` (or 结束, or `next` past the last step) ends the tour.
         */
        TourChange: {
            /**
             * Op
             * @enum {string}
             */
            op: "start" | "next" | "prev" | "stop";
            /** Steps */
            steps?: components["schemas"]["TourStep"][];
        };
        /**
         * TourFocus
         * @description What a tour step opens before it highlights: a subset of UiChanges, applied in order.
         */
        TourFocus: {
            /** View */
            view?: ("trade" | "review") | null;
            /** Panel */
            panel?: ("ai" | "depth" | "order" | "strategy" | "positions" | "news" | "debate" | "reminders") | null;
            /** Aipage */
            aiPage?: ("monitor" | "chat") | null;
            /** Aifilter */
            aiFilter?: ("all" | "chat" | "trading") | null;
            /** Positionsbar */
            positionsBar?: ("expanded" | "collapsed") | null;
            /** Positionstab */
            positionsTab?: ("positions" | "orders" | "fills" | "trades") | null;
            /** Strategytab */
            strategyTab?: ("playbooks" | "library" | "detail" | "running" | "lab") | null;
            news?: components["schemas"]["NewsNavigation"] | null;
            /** Ticketmode */
            ticketMode?: ("open" | "close" | "target") | null;
            /** Settingspage */
            settingsPage?: ("account" | "ai" | "memory" | "voice" | "chart" | "trade" | "notify" | "ui" | "shortcuts" | "data" | "about" | "remote-keys" | "remote-diagnostics" | "trade-advanced" | "voice-advanced" | "chart-advanced" | "memory-advanced" | "feedback" | "connection" | "appearance" | "simulation" | "fees") | "admin" | null;
            /** Settingsopen */
            settingsOpen?: boolean | null;
            dialog?: components["schemas"]["DialogChange"] | null;
            disclosure?: components["schemas"]["DisclosureChange"] | null;
        };
        /** TourState */
        TourState: {
            /** Active */
            active: boolean;
            /** Step */
            step: number;
            /** Total */
            total: number;
            current?: components["schemas"]["TourStep"] | null;
        };
        /** TourStep */
        TourStep: {
            /**
             * Target
             * @description 高亮目标 id（同 highlight.target）
             */
            target: string;
            /**
             * Label
             * @description 气泡里的一句话
             */
            label: string;
            /**
             * Say
             * @description 这一步要念/要说的话（语音回合逐步播报）
             */
            say?: string | null;
            /** @description 先把目标所在表面打开（view/panel/dialog…），再高亮 */
            focus?: components["schemas"]["TourFocus"] | null;
        };
        /**
         * TrailingStop
         * @description 实盘按最新价只收紧的追踪止损；value 与 activateAfter 同单位（percent=%、ticks=跳、atr_chandelier=ATR倍数）。
         */
        TrailingStop: {
            /**
             * Mode
             * @enum {string}
             */
            mode: "percent" | "ticks" | "atr_chandelier";
            /** Value */
            value: number;
            /**
             * Activateafter
             * @description 浮盈达到多少才开始追踪；0 立即追踪。
             * @default 0
             */
            activateAfter: number;
            /**
             * Atrperiod
             * @default 14
             */
            atrPeriod: number;
        };
        /**
         * UiChanges
         * @description remoteSettings 操作本机遥控器：enabled 布尔、bindings 按键到动作对象、resetBindings 布尔、diagnostic 布尔、disconnect 布尔；按住说话仅语音键，连接必须用户点击选择硬件；诊断只在测试与诊断页开始，离开自动停止，不保存或上传录音。quickVolume 设置本机快捷手数 1 / 2 / 5（快照 state.quickVolume），不下单；快捷买多、卖空、平仓、平今、平一半、撤本合约挂单、反手仅人工操作，AI 使用候选卡。每条命令仅含一个字段。多项外观或布局放在 presentation 中一次提交；settingsPage 自动打开设置（account 账户 / ai AI 与对话 / memory 记忆（画像与“我的流程”：列表 GET /api/routines，删的二次确认是 disclosure: routine-delete，id=流程 id）/ voice 语音 / chart 图表 / trade 交易 / notify 提醒（每类提醒一个开关，管对话、桌面通知、语音与锁屏推送，开关是 preference 命令；急停总是提醒）/ ui 界面 / shortcuts 快捷键 / data 数据与隐私 / about 关于，二级页 trade-advanced 高级交易设置 / voice-advanced 判停与确认时限 / remote-keys 遥控器按键设置 / remote-diagnostics 遥控器测试与诊断 / chart-advanced 指标参数与均线 / memory-advanced 约定、经验与最近变动 / feedback 问题与建议（关于下的二级页，游客也可打开；填写、复制和打开邮件草稿由用户操作，草稿不代表发送）；旧值 connection→account、appearance→ui、simulation→trade、fees→trade-advanced 仍接受）；后台管理是独立网页 qilinshuzhi.com/trader/admin，不在交易工作区窗口里，没有对应界面命令；dialog 打开/关闭对话框与抽屉（quick-order-confirm、abandon-order、protection-reprice-confirm（图上拖线改价的确认卡）、proposal-confirm（只在 1.8.0 桌面客户端，网页无此框）只能关闭；update-dialog 是新版本更新窗，仅开关窗口，下载安装仍是用户受保护操作；change-password 是设置 → 账户的修改密码框（先 settingsPage: account），只开关，密码由用户本人输入和提交，快照不含输入内容）；disclosure 展开/收起折叠段与开关（设置 → 账户：档位的每日额度与今日用量是 plan-quota；实盘开关是 live-trading——暂无期货公司接入，open: true 被拒并说明原因，快照 state.liveTrading＝{state: off, disabledReason}）；reviewSelect、selectPosition 传 null 表示取消选择；reviewDay 打开复盘（电脑是复盘页、手机是复盘摘要页）并把“今日复盘”块切到那个交易日（只能是 GET /api/account/daily-review 的 days 里的一天，否则 rejected；快照 state.review.dailyReview）；“今日复盘”块的“怎么算的”折叠是 disclosure: review-how；“习惯对照”块（实际 vs 只做计划内，GET /api/account/habits）的“怎么算的”折叠是 disclosure: habits-how，快照 state.review.habits（status、actual、planned、gap、picks 的成交 id，选中一笔用 review select）；重新生成、记住“本周最该改的一件事”是业务接口 POST /api/account/daily-review/regenerate 与 /observation；reviewCurrency CNY / USD 把账户复盘切到人民币账或外盘美元账（本站模拟合成一个账户：外盘合约在美元子账户撮合记账，顶栏权益、成交与账户明细都两边一起显示；只有有美元子账户时能切 USD，快照 state.review.currency / currencies）；highlight 高亮一个界面目标并配说明气泡，chartAnnotations 在主图画 AI 标注（set/add/clear），chartFocus 把图表滚到某时间段（null 回到最新）；selectedPrice 设置候选价并打开下单台、填入手动价格且切到限价（只填价，不改方向、不下单；快照 ticketPrice / ticketPriceMode），null 只清除候选价标记（图上左键选价已移除，五档和此命令共用入口）；panel 打开右侧栏面板（ai 对话 / strategy 策略 / news 调研（2026-10-25 前叫新闻；页签 sheet 今天（今日作战表，第一页：自选品种按优先级一行一个，关键价位、消息、可能用上的交易笔记，快照 state.news.sheet 回报 status / rows / high / mid / low / top；GET /api/research/sheet）/ desk 品种 / flash 快讯；news 相关报道（原资讯）、debate 辩论是“品种”下的子页（2026-10-31 改版第一步）；data 研究数据不再列在品种页“更多”里（基本面已含其内容），仍可直接用 tab 打开）/ debate 辩论：打开调研面板的辩论页签（原右侧栏单独的辩论页），按 panel news、news.tab debate 回报 / reminders 提醒（2026-10-25 新增；页签用 reminders {tab: mine 我的提醒 | general 通用提醒（原设置 → 提醒）| history 提醒记录（原提醒中心）}）/ order 下单：五档 + 下单票 + 本合约持仓 / positions 持仓，null 收起）；旧值 depth（原价格阶梯，已并入下单面板）仍接受，按 order 打开并按 order 回报；chartMarks:"clear" 清空当前合约图上标记（AI 标注、回测买卖点一并清除，并把成交标记的清除时间记为现在，此前的成交标记不再显示、新成交仍显示）；画线（2026-10-09 第二版，K 线与分时）：disclosure chart-drawing-toolbar 显示/收起画线工具栏（平时隐藏，浮在图表左侧；选了工具会自己出来，回十字光标就收起；手机单图上是 ✎ 底部面板；旧名 chart-drawing-tools 同义），chart-drawing-group {id: lines|levels|fib|shapes|notes|measure} 显示工具栏并展开一组工具，chart-drawing-tool {id: 工具名如 trendline/hline/fib} open 开始画（在图上点）、false 回十字光标，chart-drawing-magnet / chart-drawing-lock-all / chart-drawings-hidden 是磁吸、全部锁定、全部隐藏三个开关，chart-drawing-clear 打开“全部删除”的二次确认（这些也在主图右键“画线 ▸”里），chart-drawing-style {id: 线 id} 选中一条线并出浮动样式条（手机单图上是图下的条，mobile-drawing-style-row {id: color|width|dash} 展开其中一排颜色 / 粗细 / 线型，要先选中线），chart-drawing-settings {id: 线 id} 打开精确设置，chart-context-menu {id: main | drawing:<线 id> | pane:<kd|macd|rsi|oi>，可加 /draw、/period、/indicator、/add-pane、/style、/swap 打开子菜单} 打开三种右键菜单（只服务图表，不含下单）；画、改、删本身走 /api/market/drawings（快照 state.chartDrawings：工具栏、选中、各开关、打开的菜单与本合约的线 items）；presentation.reset 恢复默认布局；tour 启动/推进/结束分步引导（start 带 steps，每步先 focus 再 highlight）；preference 从设置页改一项账号偏好（key 点分路径 + value；agent 直接用 update_preferences 更省事；trade.aiAutonomy 与 trade.voiceOrderConfirm 不经界面命令改，只用 update_preferences）；tradingAccount 把当前会话换到目录里的另一个账户（PUT /api/sessions/{id}/account，观察见 runtime.status.account 与 UiState.tradingAccount）；strategyTab 打开策略面板并切到 library 我的策略（策略面板只有我的策略 / 正在运行两页；旧值 playbooks＝原打法页，2026-10-31 起打法改名交易笔记、搬到复盘页，按 playbooks 打开复盘页并按 view: review 回报，新建 / 改的表单是 disclosure: playbook-editor，增删改走 /api/playbooks）/ library 我的策略（“新建策略”按钮 + 每个策略一张卡）/ running 正在运行 / detail 选中策略的结果页（从卡片打开；旧值 lab＝原实验室，打开结果页的“试几组参数比一比”，回报 detail）；positionsTab 的 fills 是持仓面板与底栏的「成交」页（旧值 trades 仍接受），fillsSource 按来源筛选成交（all 全部 / manual 手动 / card AI 建议（对话里 AI 出卡、你确认的）/ ai AI 自主 / strategy 策略 / stop 止损）；aiFilter 已退役（对话面板只放对话），仍接受但只打开对话面板；strategyLaunch 填上线对话框的合约与执行方式（先 dialog: strategy-launch 打开；验证并上线由用户点）；strategyInstance 已退役：运行中实例的执行方式（alert / confirm / auto）只用 start_strategy_monitoring 改，快照 state.strategyInstances 照常；strategy-actions / strategy-delete-confirm 开关已有策略的更多操作 / 删除确认，id=library:<策略id> 或 replay:<策略id>，不执行删除；strategyEditor 操作策略编辑卡（新建或修改策略都用它，电脑放在策略面板里，手机布局是盖住底部标签的整屏页（disclosure strategy-editor-section {id: basics|entry|exit|sizing|flow} 切四段与策略流程、strategy-editor-condition {id: 组:第几条，如 entryLong:1} 开合某条条件的底部面板，strategy-editor-trades 展开预填依据）；电脑上打开时策略面板约占窗口四分之三，右栏是策略流程图与回测；disclosure strategy-editor-picker {id: 组:第几条:left|right，如 entryLong:1:left} 打开某条条件一侧的指标选择器（全部指标分类可搜索，手机打开该条的底部面板并停在那一侧），strategy-editor-sizing-more 展开④的更多，help-tip {id: 说明的 key} 打开一个“?”说明；只设计不下单）：new:true 新建（起点默认按我的习惯：近 20 个交易日已平仓成交至少 5 笔时程序按成交预填合约、方向、止损跳数、收盘前平仓、开仓时段、每次手数，带 AI 标记与依据，不够就空白；合约默认当前图表合约），edit:"<strategyId>" 修改已有策略（卡片表达不了的规则原样保留，存为新版本、原版本保留），open:true 打开（有没存的草稿接着改），start 换起点（blank / habits / template:<模板id>），ask:"一句话" 补一句（表单空时生成整份计划，已有内容时只改相关字段，改到的字段高亮、不直接保存），answers {问题 id: 回答} 回答 AI 追问，focus 把表单定位到流程图的某一块（同点方块：basics/entry/exit/sizing 或 trigger/entryLong/entryShort/action/stop/take/trailing/breakeven/scaleOut/timeTakeProfit/maxHold/closeBeforeEnd/exitRules/limits），choose 点选卡上的几项（提醒模式 repeat once 只响一次 / rearm 条件再满足再响 / bar 每根 K 线最多一次、until today / week / forever 到期、watchlist 整张自选表；策略 signalAction＝信号来了怎么办，上线对话框默认选中的执行方式，auto 要 AI 自主执行开着；快照 fields.reminder / fields.signalAction），save:true 存为草稿（开仓条件和止损填好才能存，存好电脑打开结果页、手机回到我的策略，都自动回测），close:true 关闭并放弃没存的改动；上线运行仍由用户在卡里点（打开上线对话框）。改动后约 1.5 秒自动用已收盘 K 线回测最近 60 个交易日（默认在本机浏览器算，算不了自动转云端；快照 state.strategyEditor：各字段、aiFields、prefill、backtest 四格、flow 流程图结构（触发、开多 / 开空条件组与每条满足次数、开仓、各平仓方式与回测里实际触发次数、冷却与每天次数；次数只取当前表单的预览回测，拿不到不写）、focus、canSave、missing、questions；backtest.source browser/server 与 fallbackReason、engine 本机引擎状态只供排障）；strategyBacktest 操作选中策略结果页的回测（界面只有这一种回测：规则回测，默认最近 6 个月、扣手续费；默认在本机浏览器算，算不了自动转云端，存为策略后的那次由服务端重跑）：tradingDays / slippageTicks 改参数，run:true 按当前参数回测，mark 把开平仓点画到图上或清除，trades 展开逐笔清单，applyAdvice:"<建议 id>" 按回测体检的这条建议改好、存为新版本并重跑回测（快照 state.strategyBacktest，verdict 是“AI 怎么看”：程序算的事实句与模型写的那句；source browser/server 是这次在哪算的、fallbackReason 是为何转云端、browserCheck 是服务端按同一份输入复核本机结果是否一致、engine 是本机引擎状态，只供排障，界面不显示）；strategyTemplate {restore:true} 让结果页里来自模板的策略恢复模板默认（快照 state.strategyTemplate）；模板画廊已并入策略编辑卡的“从模板”（strategyEditor start template:<id>）。strategyLab 操作结果页的“试几组参数比一比”（原实验室页签已去掉）：open 打开选中策略的结果页并停在比较，run:true 对它最关键的一个参数跑当前值和上下各一组（默认本机算、算不了转云端，前 70% 挑、后 30% 单独检验），labId 打开那次比较所属策略的结果页，choose:"<变体 id>" 把这组存为我的策略、服务端先重跑权威回测；useServer 旧字段照收（快照 state.strategyLab：每组赚 / 最多亏 / 胜率、当前组、line＝“后 X 个月单独检验：…”、where＝browser/server、fallbackReason）。停止并平仓：运行中卡的“停止并平仓”＝ POST /api/strategies/{id}/monitor/stop {closePositions:true}——停止后这个策略还有持仓就在对话里出平仓卡等用户确认，从不直接下单（自动下单的实例也一样）。help 操作使用说明卡（没开时先打开）：topic 打开主题（旧 id 仍可用），group 打开分组的第一个主题，query 搜索，detail 展开 / 收起“完整说明”，faq:{index,open} 展开 / 收起一个常见问题，retry:true 在说明读取失败后重新读取（快照 state.help，含 loadStatus loading/ready/error/offline、loadError、canRetry）；dialog: help 仍可开关卡片。view 切到 board 打开报价总表（全部品种主力合约一张表）；boardExchange 换总表的交易所筛选（all 或 watchlist 自选＝只看星标品种，FOREIGN 外盘＝海外主连报价，点一行打开它的图表（同合约选择的海外主连），快照 state.boardExchange、state.boardWatchlistCount 是自选数），boardSort 换总表排序 {key, order}（快照 state.boardSort）——筛选与排序由账号偏好 marketBoard 记住，自选品种清单是偏好 watchlist.products（“交易所.品种”如 SHFE.rb，增删先读再整段写，经 preference 或 update_preferences），列显示哪些用 disclosure: board-columns 打开“列”浮层（快照 disclosures.board-columns），增删调序经 preference: marketBoard.columns 或 update_preferences；contractProduct 展开合约抽屉里某个品种的月份列表（“<交易所>.<品种>”如 SHFE.rb，海外主连用外盘品种键如 COMEX.GC，需抽屉已打开，null 收起全部，快照 state.contractProduct）；contractCatalogRetry: true 等价抽屉里的“重试”，目录读取失败或读到空目录后重新读取（需抽屉已打开、快照 state.contractCatalog.canRetry 为 true；正在读取、已读到、应用服务未连接或游客会话失效时 rejected（命令回执只有状态，原因读快照 state.contractCatalog.status））。duration 切 K 线周期（60/180/300/900/1800/3600/86400/604800 秒 = 1/3/5/15/30 分、1 小时、日线、周线），任何周期都会回到 K 线视图；chartView 切图表形态：timeshare 打开分时图（当交易日分钟收盘折线＋当日均价＋昨结虚线，数据周期钉在 60 秒，无 MA/副图指标）、kline 回 K 线（快照 state.chartView）。mobileTab 切手机版底部标签（只对手机布局的窗口有效，电脑布局 rejected）：ai AI 首页（今天概况、待确认置顶、带筛选的时间线）、chat AI 对话（与 panel: ai 同一状态）、research 调研、market 看盘、strategy 策略（正在运行 / 我的策略两个分页 + 底部“跟 AI 说……”输入框；分页用 mobileStrategyTab；我的策略顶上“新建策略”、每条“编辑 / 回测 / 上线…”，新建与编辑用 strategyEditor）、me 我的；旧值 notify（原通知标签，已并进 AI 首页）仍接受，打开 AI 首页并保持当前筛选，快照回报 mobileTab ai（快照 state.mobileLayout、state.mobileTab；手机布局下打开侧边面板的命令 rejected；没登录的游客窗口也是手机布局，默认 market，只能看盘和调研，ai / strategy / me 显示“登录后使用”页，chat 不可用）。mobilePage 打开手机版的全屏子页（只对手机布局有效）：order 下单页（当前合约，确认后才下单）、positions 持仓与委托、review 复盘摘要、briefs AI 简报推送（推送开关与订阅品种，增删调序走 PUT /api/app/brief-subscriptions）、install 安装到桌面引导（按浏览器给步骤、安卓一键安装按钮或微信等 App 内的换浏览器遮罩；快照 state.installEnvironment 是识别出的环境 installed / in-app-ios / in-app-android / ios-safari-26 / ios-safari-legacy / ios-chrome-edge / ios-other / android-prompt / android-vendor / desktop；安装本身由用户在浏览器里完成）、reminders 提醒（提醒中心的手机页，同 reminderCenter: {open: true}）、push 锁屏提醒（这台设备的开关与“发一条试试”、已开锁屏提醒的设备；提醒哪些事与锁屏上显示金额和手数在 settingsPage notify；快照 state.pushSupport＝supported / needs-install（iPhone 要先放到主屏幕）/ unsupported（安卓与 Chrome 走谷歌推送，国内收不到），state.pushSubscribed＝这台设备是否已开；打开必须用户在页面上点，AI 只能用 GET /api/app/push 查、POST /api/app/push/test 发一条试试）、lab 回测结果页（策略 → 最近的回测里最新一次；几组参数的净值、样本内 / 样本外与过拟合风险，选用一组走 POST /api/strategy-labs/{labId}/choose，“上线运行”与电脑同一校验、执行方式固定出卡确认；要看指定的一次用 mobileLab）、accounts 设置·账户（交易账户切换）、memory 设置·记忆（AI 与记忆，含我的流程）、appearance 设置·界面（外观与字号）、help 使用说明卡，null 关闭（快照 state.mobilePage）；手机布局下 positionsTab / fillsSource / ordersHistory 落到持仓与委托页、ticketMode / selectPosition 落到下单页、reviewExpand / reviewFilter / reviewSelect / reviewCurrency 落到复盘摘要页；看盘的合约选择是 dialog: contract-drawer，周期与合约用 duration / chartView / symbol。mobileNotifyFilter 切 AI 首页时间线的筛选（原通知页）并打开 AI 标签、滚到时间线：all 全部、fill 成交、brief AI 简报；旧值 reminder 打开提醒页（快照 state.mobileNotifyFilter；state.mobileUnread 是提醒中心的未读条数，与 state.reminderCenter.unread 相同）。reminders {tab} 打开提醒面板（电脑右侧栏“提醒”）/ 提醒页（手机）并切到该页签：mine 我的提醒（一张表：到某个价、碰到我画的线（到价提醒，增删走 /api/market/alerts，画线的带 drawingId）和按条件（条件提醒：只提醒的策略，快照 state.myReminders {status, items: [{id, name, status monitoring/paused/idle（任一合约在盯即 monitoring）, symbol（在跑的各合约）, runs [{runId, status}]}]}，暂停 / 删除作用于每个实例，改用 strategyEditor {edit: id}，开停删走策略接口 monitor/start {signalAction: alert}、monitor/pause、DELETE /api/strategy-plans/{id}）；“新建提醒”= disclosure: reminder-alert-new（到某个价的表单，当前合约），旁边“按条件…”= strategyEditor {new: true, reminder: true}）、general 通用提醒（每类提醒的开关、桌面通知、锁屏推送；原设置 → 提醒，旧命令 settingsPage: notify 也到这里）、history 提醒记录；快照 state.reminders {tab}（面板或页面开着时）。reminderCenter {open?, filter?, more?} 打开/关闭提醒记录——电脑是提醒面板的“提醒记录”页签（K 线工具条铃铛直达），手机是提醒页的同一页签（同一份数据）：filter unread 只看未读、all 当天全部（只给 filter 也会打开）；more: true＝“继续加载当天提醒”，当天一次最多读 500 条，快照 state.reminderCenter.moreToday 为 true 时读下一批（同时打开提醒中心）：下一批读到才回 applied（moreDone 追上 moreAsked 且 moreError 为空），没有更多、刚刷新过要再点或读取失败回 rejected，原因在 state.reminderCenter.moreError；内容是当前交易日的提醒按日盘上午 / 下午 / 夜盘分组倒序，“查看更早”与已读走业务接口 GET /api/reminders?scope=earlier、POST /api/reminders/read（快照 state.reminderCenter {open, filter, unread, today, moreToday, moreAsked, moreDone, moreError, earlier}，today 是已读出的当天条数）；点一条提醒去它的卡（对话）、委托、复盘、调研或新闻；保本提醒的“移到成本 / 不用”是 POST /api/ai-execution/breakeven/{id}/{move|decline}。mobileNotifyProduct 在 AI 首页时间线的 AI 简报下只看一个品种（“<交易所>.<品种>”如 SHFE.rb，须是订阅品种（推送范围内）或时间线里出现的品种，否则 rejected；null 看全部品种；同时切到 AI 简报筛选与 AI 标签，快照 state.mobileNotifyProduct）。mobileLab {labId} 打开手机版回测结果页看指定的一次比较（labId 来自 GET /api/strategy-labs；快照 state.mobilePage lab、state.mobileLab {labId, chosen}——chosen 是已选用的变体，未选为 null）。mobileStrategyTab 切手机版策略页的分页（running 正在运行：暂停 / 继续 / 停止并平仓；mine 我的策略：顶上“新建策略”，每个策略一张卡带“编辑 / 回测 / 上线…”，一条没有时是三种开始方式与常用模板），先切到策略标签（快照 state.mobileStrategyTab）。mobileMarket 改手机版看盘（只对手机布局有效，先切到看盘标签；一次一项）：view quotes 报价列表 / multi 多图 / single 单图；board 报价板块 watchlist 自选 / main 主力 / SHFE 上期所 / DCE 大商所 / CZCE 郑商所 / CFFEX 中金所 / GFEX 广期所 / INE 能源中心 / FOREIGN 外盘（点一行打开它的单图；切到报价）；columns 报价的数值列 1–4 个（last 最新 / changePercent 涨跌幅 / change 涨跌 / openInterest 持仓量 / bid 买价 / ask 卖价 / volume 成交量 / openInterestChange 日增仓）；layout 多图布局 four 4 合约 / periods 同合约 4 周期 / six 6 图（切到多图）；cell {index, symbol?, duration?} 改多图当前布局的一格（symbol null＝自动补位；同合约 4 周期里 symbol 切当前合约）；subPane 单图下面显示哪个副图（kd / macd / rsi / oi 持仓量 / cci / volume 成交量，未选的替换正在显示的那个）；subPanes true/false 开关副图设置面板（选 0–3 个副图、主图指标 均线 / 布林 / SAR、各指标参数，快照 disclosures.mobile-subpanes）。都记在账号偏好 ui.mobileMarket 与 chart.subPaneList，快照 state.preferences。researchDebate 操作“多 AI 辩论”页（电脑是调研面板“品种”下的辩论子页，同 panel: debate；手机是调研页上的整屏子页；一次一项）：start 发起或打开辩论——start: "desk" 等于今日判断旁的“让 AI 们辩一轮”（今日议题），start: "event" 辩当前品种最新一条重要快讯，start: <事件 id>（快讯接口 events[].id）等于快讯行的“研讨”；同一事件同一品种当天只辩一次、今日议题资料没变时直接打开已有结果；open: <研讨 id> 打开一次辩论（也是“以前的辩论”里的一格）；rerun: true 是结果页的“再辩一轮”（新跑一次，计入当日次数）；close: true 收起辩论页。辩论本身走 POST /api/research/debates（技术面、供需基本面、资金持仓、新闻宏观四位分析师先同时写报告，再多方开场、空方回应、多方反驳、空方反驳，一句一句流式出现，最后裁判匿名裁决，约 2 到 3 分钟；写完的报告卡片用 disclosure: debate-analyst-technical / supply / flow / news 展开收起），写进今日判断走 POST /api/research/debates/{id}/adopt，停止走 POST /api/research/debates/{id}/cancel（快照 state.researchDebate：id、status queued/running/done/review 需复核/failed/cancelled、step（collect / analysts / turn1…turn4 / judge）、analysts（四份报告写完几份，没写成也算）、lesson（上次复盘）、speaking（正在说的一方）、topic、stance、confidence、canAdopt、adopted、request）。游客不能发起。
         */
        UiChanges: {
            /** Symbol */
            symbol?: string | null;
            /** Duration */
            duration?: (60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800) | null;
            /** Chartview */
            chartView?: ("kline" | "timeshare") | null;
            /** View */
            view?: ("trade" | "review" | "board") | null;
            /** Panel */
            panel?: ("ai" | "depth" | "order" | "strategy" | "positions" | "news" | "debate" | "reminders") | null;
            /** Settingsopen */
            settingsOpen?: boolean | null;
            /** Theme */
            theme?: ("dark" | "light" | "system") | null;
            /** Dockside */
            dockSide?: ("left" | "right") | null;
            /** Dockwidth */
            dockWidth?: number | null;
            /** Positionsbar */
            positionsBar?: ("expanded" | "collapsed") | null;
            /** Settingspage */
            settingsPage?: ("account" | "ai" | "memory" | "voice" | "chart" | "trade" | "notify" | "ui" | "shortcuts" | "data" | "about" | "remote-keys" | "remote-diagnostics" | "trade-advanced" | "voice-advanced" | "chart-advanced" | "memory-advanced" | "feedback" | "connection" | "appearance" | "simulation" | "fees") | null;
            /** Fontscale */
            fontScale?: ("standard" | "large" | "extra-large") | null;
            /** Density */
            density?: ("compact" | "standard" | "comfortable") | null;
            chartOption?: components["schemas"]["ChartOptionChange"] | null;
            chartSizes?: components["schemas"]["ChartSizes"] | null;
            strategy?: components["schemas"]["StrategyUiChange"] | null;
            presentation?: components["schemas"]["PresentationChanges"] | null;
            pinnedSymbols?: components["schemas"]["PinnedSymbolsChange"] | null;
            /** Aipage */
            aiPage?: ("monitor" | "chat") | null;
            /** Aifilter */
            aiFilter?: ("all" | "chat" | "trading") | null;
            /** Aiactivitycard */
            aiActivityCard?: ("open" | "closed") | null;
            /** Positionstab */
            positionsTab?: ("positions" | "orders" | "fills" | "trades") | null;
            /** Fillssource */
            fillsSource?: ("all" | "manual" | "card" | "ai" | "strategy" | "stop" | "liquidation") | null;
            /** Strategytab */
            strategyTab?: ("playbooks" | "library" | "detail" | "running" | "lab") | null;
            news?: components["schemas"]["NewsNavigation"] | null;
            reminders?: components["schemas"]["RemindersNavigation"] | null;
            /** Ordershistory */
            ordersHistory?: boolean | null;
            /** Ticketmode */
            ticketMode?: ("open" | "close" | "target") | null;
            /** Quickorderconfirm */
            quickOrderConfirm?: boolean | null;
            remoteSettings?: components["schemas"]["RemoteSettingsChange"] | null;
            /** Quickvolume */
            quickVolume?: (1 | 2 | 5) | null;
            chatSession?: components["schemas"]["ChatSessionChange"] | null;
            reviewExpand?: components["schemas"]["ReviewExpandChange"] | null;
            /** Reviewfilter */
            reviewFilter?: string | null;
            /** Reviewselect */
            reviewSelect?: string | null;
            /** Reviewday */
            reviewDay?: string | null;
            /** Reviewcurrency */
            reviewCurrency?: ("CNY" | "USD") | null;
            dialog?: components["schemas"]["DialogChange"] | null;
            strategyDetails?: components["schemas"]["StrategyDetailsChange"] | null;
            monitorCard?: components["schemas"]["MonitorCardChange"] | null;
            strategyLaunch?: components["schemas"]["StrategyLaunchChange"] | null;
            strategyInstance?: components["schemas"]["StrategyInstanceChange"] | null;
            strategyBacktest?: components["schemas"]["StrategyBacktestChange"] | null;
            strategyTemplate?: components["schemas"]["StrategyTemplateChange"] | null;
            /** Strategyguide */
            strategyGuide?: {
                [key: string]: unknown;
            } | null;
            strategyLab?: components["schemas"]["StrategyLabChange"] | null;
            /** Strategyidea */
            strategyIdea?: {
                [key: string]: unknown;
            } | null;
            strategyEditor?: components["schemas"]["StrategyEditorChange"] | null;
            help?: components["schemas"]["HelpChange"] | null;
            /** Boardexchange */
            boardExchange?: ("all" | "watchlist" | "SHFE" | "INE" | "DCE" | "CZCE" | "CFFEX" | "GFEX" | "FOREIGN") | null;
            boardSort?: components["schemas"]["BoardSortChange"] | null;
            /** Contractproduct */
            contractProduct?: string | null;
            /** Contractcatalogretry */
            contractCatalogRetry?: true | null;
            researchDebate?: components["schemas"]["ResearchDebateChange"] | null;
            selectPosition?: components["schemas"]["PositionSelection"] | null;
            disclosure?: components["schemas"]["DisclosureChange"] | null;
            /** Selectedprice */
            selectedPrice?: number | null;
            /** Chartmarks */
            chartMarks?: "clear" | null;
            highlight?: components["schemas"]["HighlightChange"] | null;
            chartAnnotations?: components["schemas"]["ChartAnnotationsChange"] | null;
            chartFocus?: components["schemas"]["ChartFocusChange"] | null;
            tour?: components["schemas"]["TourChange"] | null;
            preference?: components["schemas"]["PreferenceChange"] | null;
            /** Tradingaccount */
            tradingAccount?: string | null;
            /** Mobiletab */
            mobileTab?: ("ai" | "chat" | "research" | "market" | "strategy" | "me" | "notify") | null;
            /** Mobilepage */
            mobilePage?: ("order" | "positions" | "review" | "briefs" | "install" | "push" | "lab" | "reminders" | "accounts" | "memory" | "appearance" | "help") | null;
            /** Mobilenotifyfilter */
            mobileNotifyFilter?: ("all" | "reminder" | "fill" | "brief") | null;
            /** Mobilenotifyproduct */
            mobileNotifyProduct?: string | null;
            mobileLab?: components["schemas"]["MobileLabChange"] | null;
            /** Mobilestrategytab */
            mobileStrategyTab?: ("running" | "mine") | null;
            mobileMarket?: components["schemas"]["MobileMarketChange"] | null;
            reminderCenter?: components["schemas"]["ReminderCenterChange"] | null;
        };
        /** UiCommand */
        UiCommand: {
            /**
             * Commandid
             * Format: uuid
             */
            commandId: string;
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /** Expectedrevision */
            expectedRevision: number;
            changes: components["schemas"]["UiChanges"];
        };
        /** UiCommandResult */
        UiCommandResult: {
            /**
             * Commandid
             * Format: uuid
             */
            commandId: string;
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /** Expectedrevision */
            expectedRevision: number;
            changes: components["schemas"]["UiChanges"];
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "dispatched" | "applied" | "rejected" | "expired" | "unknown";
            state: components["schemas"]["UiState"] | null;
            /** Observedrevision */
            observedRevision?: number | null;
        };
        /** UiDevice */
        UiDevice: {
            /** Deviceid */
            deviceId: string;
            /**
             * Platform
             * @enum {string}
             */
            platform: "macos" | "windows" | "linux" | "web";
            /** Version */
            version: string;
            /** Build */
            build?: string | null;
            /**
             * Nativecapture
             * @default false
             */
            nativeCapture: boolean;
        };
        /** UiObservation */
        UiObservation: {
            /** Capturedat */
            capturedAt: string;
            /** Viewport */
            viewport: {
                [key: string]: number;
            };
            /** Activedialog */
            activeDialog?: string | null;
            /** Text */
            text: string;
            /** Truncated */
            truncated: boolean;
            /** Regions */
            regions: {
                [key: string]: unknown;
            }[];
            /** Controls */
            controls: {
                [key: string]: unknown;
            }[];
            /** Context */
            context: {
                [key: string]: unknown;
            };
        };
        /** UiPulse */
        UiPulse: {
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /** Revision */
            revision: number;
            state: components["schemas"]["UiState"];
            receipt?: components["schemas"]["UiReceipt"] | null;
            observation: components["schemas"]["UiObservation"];
            /**
             * Closing
             * @default false
             */
            closing: boolean;
            device?: components["schemas"]["UiDevice"] | null;
            captureReceipt?: components["schemas"]["CaptureReceipt"] | null;
            /** Visible */
            visible?: boolean | null;
        };
        /** UiPulseResult */
        UiPulseResult: {
            command: components["schemas"]["UiCommandResult"] | null;
            capture?: components["schemas"]["CaptureRequest"] | null;
        };
        /** UiReceipt */
        UiReceipt: {
            /**
             * Commandid
             * Format: uuid
             */
            commandId: string;
            /**
             * Status
             * @enum {string}
             */
            status: "applied" | "rejected" | "unknown";
        };
        /** UiState */
        UiState: {
            /** Symbol */
            symbol?: string | null;
            /**
             * Duration
             * @enum {integer}
             */
            duration: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
            /** Chartview */
            chartView?: ("kline" | "timeshare") | null;
            /**
             * View
             * @enum {string}
             */
            view: "watch" | "trade" | "review" | "admin" | "board";
            /** Panel */
            panel: ("ai" | "depth" | "order" | "strategy" | "positions" | "news" | "debate" | "reminders") | null;
            /** Settingsopen */
            settingsOpen: boolean;
            /**
             * Theme
             * @enum {string}
             */
            theme: "dark" | "light" | "system";
            /**
             * Dockside
             * @enum {string}
             */
            dockSide: "left" | "right";
            /** Dockwidth */
            dockWidth: number;
            /** Replay */
            replay: boolean;
            /** Settingspage */
            settingsPage: ("account" | "ai" | "memory" | "voice" | "chart" | "trade" | "notify" | "ui" | "shortcuts" | "data" | "about" | "remote-keys" | "remote-diagnostics" | "trade-advanced" | "voice-advanced" | "chart-advanced" | "memory-advanced" | "feedback" | "connection" | "appearance" | "simulation" | "fees") | "admin";
            /**
             * Fontscale
             * @enum {string}
             */
            fontScale: "standard" | "large" | "extra-large";
            /**
             * Density
             * @enum {string}
             */
            density: "compact" | "standard" | "comfortable";
            chart: components["schemas"]["ChartOptions"];
            chartSizes: components["schemas"]["ChartSizes"];
            strategy: components["schemas"]["StrategyUiState"] | null;
            /** Positionsbar */
            positionsBar?: ("expanded" | "collapsed") | null;
            /** Positionsbarautohidden */
            positionsBarAutoHidden?: boolean | null;
            /** Pinnedsymbols */
            pinnedSymbols?: string[] | null;
            /** Aipage */
            aiPage?: ("monitor" | "chat") | null;
            /** Aifilter */
            aiFilter?: ("all" | "chat" | "trading") | null;
            /** Aiactivitycard */
            aiActivityCard?: ("open" | "closed") | null;
            /** Positionstab */
            positionsTab?: ("positions" | "orders" | "fills" | "trades") | null;
            /** Fillssource */
            fillsSource?: ("all" | "manual" | "card" | "ai" | "strategy" | "stop" | "liquidation") | null;
            /** Strategytab */
            strategyTab?: ("playbooks" | "library" | "detail" | "running" | "lab") | null;
            /** News */
            news?: {
                [key: string]: unknown;
            } | null;
            /** Reminders */
            reminders?: {
                [key: string]: unknown;
            } | null;
            /** Myreminders */
            myReminders?: {
                [key: string]: unknown;
            } | null;
            /** Researchdebate */
            researchDebate?: {
                [key: string]: unknown;
            } | null;
            /** Ordershistory */
            ordersHistory?: boolean | null;
            /** Ticketmode */
            ticketMode?: ("open" | "close" | "target") | null;
            /** Quickorderconfirm */
            quickOrderConfirm?: boolean | null;
            /** Remotesettings */
            remoteSettings?: {
                [key: string]: unknown;
            } | null;
            /** Quickvolume */
            quickVolume?: (1 | 2 | 5) | null;
            review?: components["schemas"]["ReviewUiState"] | null;
            /** Chatsessionid */
            chatSessionId?: string | null;
            /** Chathistory */
            chatHistory?: components["schemas"]["ChatHistoryItem"][] | null;
            selectedPosition?: components["schemas"]["PositionSelection"] | null;
            /** Dialog */
            dialog?: string | null;
            /** Strategydetails */
            strategyDetails?: {
                [key: string]: boolean;
            } | null;
            /** Strategysummary */
            strategySummary?: string | null;
            /** Monitorcards */
            monitorCards?: {
                [key: string]: "expanded" | "collapsed";
            } | null;
            /** Disclosures */
            disclosures?: {
                [key: string]: boolean;
            } | null;
            /** Selectedprice */
            selectedPrice?: number | null;
            /** Ticketprice */
            ticketPrice?: number | null;
            /** Ticketpricemode */
            ticketPriceMode?: ("limit" | "opponent" | "last") | null;
            /** Markscleared */
            marksCleared?: string | null;
            /** Highlights */
            highlights?: {
                [key: string]: unknown;
            }[] | null;
            /** Chartannotations */
            chartAnnotations?: {
                [key: string]: unknown;
            }[] | null;
            /** Chartfocus */
            chartFocus?: {
                [key: string]: number;
            } | null;
            tour?: components["schemas"]["TourState"] | null;
            /** Preferences */
            preferences?: {
                [key: string]: unknown;
            } | null;
            /** Tradingaccount */
            tradingAccount?: string | null;
            /** Adminsection */
            adminSection?: ("overview" | "users" | "plans" | "skills" | "invites" | "accounts") | null;
            /** Strategylaunch */
            strategyLaunch?: {
                [key: string]: unknown;
            } | null;
            /** Strategyinstances */
            strategyInstances?: {
                [key: string]: unknown;
            }[] | null;
            /** Strategybacktest */
            strategyBacktest?: {
                [key: string]: unknown;
            } | null;
            /** Strategytemplate */
            strategyTemplate?: {
                [key: string]: unknown;
            } | null;
            /** Strategyguide */
            strategyGuide?: {
                [key: string]: unknown;
            } | null;
            /** Strategyeditor */
            strategyEditor?: {
                [key: string]: unknown;
            } | null;
            /** Help */
            help?: {
                [key: string]: unknown;
            } | null;
            /** Strategylab */
            strategyLab?: {
                [key: string]: unknown;
            } | null;
            /** Strategyidea */
            strategyIdea?: {
                [key: string]: unknown;
            } | null;
            /** Mobilestrategytab */
            mobileStrategyTab?: ("running" | "mine") | null;
            /** Boardexchange */
            boardExchange?: string | null;
            /** Boardsort */
            boardSort?: {
                [key: string]: unknown;
            } | null;
            /** Boardwatchlistcount */
            boardWatchlistCount?: number | null;
            /** Contractproduct */
            contractProduct?: string | null;
            /** Contractcatalog */
            contractCatalog?: {
                [key: string]: unknown;
            } | null;
            /** Chartdrawings */
            chartDrawings?: {
                [key: string]: unknown;
            } | null;
            /** Mobilelayout */
            mobileLayout?: boolean | null;
            /** Mobiletab */
            mobileTab?: ("ai" | "chat" | "research" | "market" | "strategy" | "me" | "notify") | null;
            /** Mobilepage */
            mobilePage?: ("order" | "positions" | "review" | "briefs" | "install" | "push" | "lab" | "reminders" | "accounts" | "memory" | "appearance" | "help") | null;
            /** Mobilenotifyfilter */
            mobileNotifyFilter?: ("all" | "reminder" | "fill" | "brief") | null;
            /** Mobileunread */
            mobileUnread?: number | null;
            /** Mobilenotifyproduct */
            mobileNotifyProduct?: string | null;
            mobileLab?: components["schemas"]["MobileLabState"] | null;
            /** Installenvironment */
            installEnvironment?: ("installed" | "in-app-ios" | "in-app-android" | "ios-safari-26" | "ios-safari-legacy" | "ios-chrome-edge" | "ios-other" | "android-prompt" | "android-vendor" | "desktop") | null;
            /** Pushsupport */
            pushSupport?: ("supported" | "needs-install" | "unsupported") | null;
            liveTrading?: components["schemas"]["LiveTradingState"] | null;
            reminderCenter?: components["schemas"]["ReminderCenterState"] | null;
            memory?: components["schemas"]["MemoryPanelState"] | null;
            routines?: components["schemas"]["RoutinesPanelState"] | null;
            /** Pushsubscribed */
            pushSubscribed?: boolean | null;
        };
        /** UiWindowDetail */
        UiWindowDetail: {
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /** Revision */
            revision: number;
            /** Online */
            online: boolean;
            state: components["schemas"]["UiState"];
            device?: components["schemas"]["UiDevice"] | null;
            observation: components["schemas"]["UiObservation"];
        };
        /** UiWindowSnapshot */
        UiWindowSnapshot: {
            /**
             * Windowid
             * Format: uuid
             */
            windowId: string;
            /** Revision */
            revision: number;
            /** Online */
            online: boolean;
            state: components["schemas"]["UiState"];
            device?: components["schemas"]["UiDevice"] | null;
        };
        /** UiWindowsSnapshot */
        UiWindowsSnapshot: {
            /** Windows */
            windows: components["schemas"]["UiWindowSnapshot"][];
        };
        /** ValidatePlanRequest */
        ValidatePlanRequest: {
            /** Symbol */
            symbol?: string | null;
            /** Charttimeframeseconds */
            chartTimeframeSeconds?: (60 | 180 | 300 | 900 | 3600 | 86400) | null;
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
        /** WorkbenchConfig */
        WorkbenchConfig: {
            /**
             * Showaiactivity
             * @default true
             */
            showAiActivity: boolean;
            /**
             * Showtrademarkers
             * @default true
             */
            showTradeMarkers: boolean;
            /**
             * Showmarketbriefs
             * @default true
             */
            showMarketBriefs: boolean;
            /** Extensions */
            extensions?: components["schemas"]["ChartExtension"][];
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    explain_api_app_strategy_explanations_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExplanationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ExplanationResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    explanation_api_app_strategy_explanations__id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ExplanationResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    request_capture_api_app_ui_windows__window_id__captures_post: {
        parameters: {
            query?: {
                waitMs?: number;
            };
            header?: never;
            path: {
                window_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CaptureRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CaptureResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    read_capture_api_app_ui_captures__request_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                request_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CaptureResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    pulse_api_ui_pulse_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UiPulse"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiPulseResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_windows_api_app_ui_windows_get: {
        parameters: {
            query?: {
                deviceId?: string | null;
                windowId?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiWindowsSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    window_detail_api_app_ui_windows__window_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                window_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiWindowDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    enqueue_api_app_ui_commands_post: {
        parameters: {
            query?: {
                waitMs?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UiCommand"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiCommandResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    command_status_api_app_ui_commands__command_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                command_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiCommandResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    push_status_api_app_push_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PushStatus"];
                };
            };
        };
    };
    save_push_subscription_api_app_push_subscription_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PushSubscribe"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PushStatus"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_push_subscription_api_app_push_subscription_delete: {
        parameters: {
            query: {
                /** @description 要移除的设备（本机或已开的另一台） */
                deviceId: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PushStatus"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    test_push_api_app_push_test_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PushTest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PushTestResults"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    read_preferences_api_app_preferences_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PreferencesSnapshot"];
                };
            };
        };
    };
    save_preferences_api_app_preferences_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PreferencesUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PreferencesSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    app_help_api_app_help_get: {
        parameters: {
            query?: {
                topic?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HelpSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    app_activity_api_app_activity_get: {
        parameters: {
            query?: {
                symbol?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ActivitySnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    app_config_api_app_config_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigSnapshot"];
                };
            };
        };
    };
    save_app_config_api_app_config_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfigUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    app_briefs_api_app_briefs_get: {
        parameters: {
            query?: {
                symbol?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BriefsSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    refresh_app_briefs_api_app_briefs_refresh_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BriefRefresh"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BriefRefreshResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    app_brief_subscriptions_api_app_brief_subscriptions_get: {
        parameters: {
            query?: {
                symbol?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BriefSubscriptionsSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    save_app_brief_subscriptions_api_app_brief_subscriptions_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BriefSubscriptionsUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BriefSubscriptionsSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    rollback_app_config_api_app_config_rollback_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfigRollback"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigSnapshot"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retired_guide_get_guide_api_strategy_plans_guide_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    retired_guide_post_guide_api_strategy_plans_guide_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retired_guide_delete_guide_api_strategy_plans_guide_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    retired_guide_post_answer_api_strategy_plans_guide_answer_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retired_guide_post_confirm_api_strategy_plans_guide_confirm_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retired_guide_post_pause_api_strategy_plans_guide_pause_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    retired_guide_post_undo_api_strategy_plans_guide_undo_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_costs_api_trading_costs_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FeeSchedule"];
                };
            };
        };
    };
    save_costs_api_trading_costs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FeeSchedule"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FeeSchedule"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    workspace_identity_api_workspace_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    start_design_api_strategy_plans_design_jobs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DesignJobRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    design_status_api_strategy_plans_design_jobs__identifier__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stop_design_api_strategy_plans_design_jobs__identifier__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    design_api_strategy_plans_design_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DesignRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["StrategyPlan"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    library_api_strategy_plans_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    save_plan_api_strategy_plans_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SavePlanRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    habit_prefill_route_api_strategy_plans_prefill_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    delete_plan_api_strategy_plans__identifier__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_scope_api_strategy_plans__identifier__scope_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InstrumentScope"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    apply_health_advice_api_strategy_plans__identifier__apply_advice_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApplyAdviceRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    chart_period_api_strategy_plans_chart_period_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChartPeriodRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    validate_plan_api_strategy_plans__identifier__validate_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ValidatePlanRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    conditions_api_strategy_plans__identifier__conditions_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identifier: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backtest_strategy_api_strategies__strategy_id__backtest_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                strategy_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BacktestOptions"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    recent_backtests_api_strategy_backtests_get: {
        parameters: {
            query?: {
                strategyId?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backtest_plan_api_strategy_backtests_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PlanBacktestRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    browser_inputs_api_strategy_backtests_inputs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BrowserInputsRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    browser_result_api_strategy_backtests_browser_result_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BrowserResultRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backtest_verdict_api_strategy_backtests__backtest_id__verdict_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                backtest_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backtest_queue_api_strategy_backtests_queue_get: {
        parameters: {
            query?: {
                requestId?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_templates_api_strategy_templates_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_template_api_strategy_templates__template_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    preview_template_api_strategy_templates__template_id__preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TemplatePreviewRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    instantiate_template_api_strategy_templates__template_id__instantiate_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TemplateInstantiateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    trade_review_api_trade_review_get: {
        parameters: {
            query?: {
                symbol?: string | null;
                days?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
