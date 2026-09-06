-- 合规内核 V0 · 索引数据 schema
--
-- 正本：businessSYETEM/V0_开发交接包.md 第二节「索引数据 schema」
-- 对应任务：V0_执行规划 T1 / 执行序 S0
--
-- 四张表：source 对应 K1 语料与权利登记，clause 对应 K2 条款单元，
-- 另加关系表 clause_link 与冲突表 conflict。
--
-- 字段的增删与语义变更属于契约变更，须回产品审核确认（产研交接 第 5 节）。
-- 本文件在交接包 DDL 之上只补充了 check 约束与索引——前者把交接包正文里
-- 已用文字写死的取值域落成数据库约束，后者属实现细节（产研交接 第 5 节「可以自己定」）。

begin;

-- ---------------------------------------------------------------------------
-- K1 规范来源登记
-- ---------------------------------------------------------------------------
create table if not exists source (
  id                text primary key,          -- ppwr_reg / ppwr_guidance / ppwr_faq / dec_2026_429
  title             text not null,
  instrument_class  text not null,             -- regulation / delegated_act / guidance / faq
  authority_rank    smallint not null,         -- 1 法规与附件, 2 授权与实施法案, 3 指南, 4 FAQ
  eli               text,
  oj_ref            text,
  version           text not null,
  lang_authenticity text not null,             -- ORIGINAL_AUTHENTIC 等四档
  rights_status     text not null default 'UNKNOWN',   -- ALLOW / DENY / UNKNOWN
  ingested_at       timestamptz not null,

  constraint source_instrument_class_ck
    check (instrument_class in ('regulation', 'delegated_act', 'guidance', 'faq')),
  -- 规范 5.2 效力位阶四级
  constraint source_authority_rank_ck
    check (authority_rank between 1 and 4),
  -- 规范 5.3 语言真实性四档
  constraint source_lang_authenticity_ck
    check (lang_authenticity in ('ORIGINAL_AUTHENTIC',
                                 'OFFICIAL_TRANSLATION',
                                 'REVIEWED_WORKING_TRANSLATION',
                                 'MACHINE_TRANSLATION_UNVERIFIED')),
  -- 规范 6.4 权利闸：UNKNOWN 默认等于拒绝
  constraint source_rights_status_ck
    check (rights_status in ('ALLOW', 'DENY', 'UNKNOWN'))
);

comment on table source is
  'K1 语料与权利登记。rights_status 非 ALLOW 的来源不得被检索、抽取、向量化或送入模型上下文（红线第 12 条）。';
comment on column source.authority_rank is
  '1 法规正文与附件 / 2 授权与实施法案 / 3 官方实施指南 / 4 FAQ。1、2 有法律约束力，3、4 是委员会解释。';
comment on column source.rights_status is
  'UNKNOWN 默认按拒绝处理（规范 6.4、上游 C-L2N 12.1）。V0 四份来源均为欧盟公开法源，登记后置为 ALLOW。';

-- ---------------------------------------------------------------------------
-- K2 条款单元
-- ---------------------------------------------------------------------------
create table if not exists clause (
  id                 text primary key,         -- ppwr/art/5/5/c
  source_id          text not null references source(id),
  path_article       text,                     -- '5'
  path_paragraph     text,                     -- '5'
  path_point         text,                     -- 'c'
  annex              text,                     -- 附件条目用, 如 'II/table3'
  heading            text,
  -- 作准文本。交接包 DDL 写的是 not null，但署名断言（is_inference = true）按定义
  -- 就是「官方文件未覆盖」，没有作准文本可锚——assertions.yaml 里既无 src 也无行号。
  -- 因此放宽为可空，另用 clause_text_en_required_ck 保证非断言条目必填，契约意图不变。
  -- **这是对字段语义的加限放宽，须产品侧确认**（产研交接 第 5 节）。
  text_en            text,
  text_zh            text,                     -- 工作译述
  lang_authenticity_zh text default 'REVIEWED_WORKING_TRANSLATION',
  page_from          int,
  bbox               jsonb,                    -- 高亮框, 归一化 0..1, 原点左上
  page_to            int,
  line_from          int,                      -- 回溯 _text_clean 提取件
  line_to            int,
  scope              text,                     -- all_packaging / food_contact_only /
                                               -- transport_only / not_product_scoped
  effective_expr     jsonb,                    -- 生效日表达式与法定截止日（deadlines 键）
  numeric_limits     jsonb,                    -- [{name, value, unit, basis}]
  pending_dependency jsonb,                    -- 未出台依赖：挂钩哪份法案、有无法定截止日、在此之前按什么执行
  notes              text,                     -- 答题纪律。随条款进模型上下文，但不进 /api/clause/{id}
  text_en_override   text,                     -- 提取件有缺陷时的人工原文。全批仅 PFAS 定义段一处
  assertion_body     jsonb,                    -- 署名断言正文，仅 is_inference 时非空
  is_inference       boolean not null default false,
  adopted_by         text,
  adopted_at         timestamptz,
  status             text not null default 'draft',   -- draft / adopted
  version            int not null default 1,

  constraint clause_status_ck
    check (status in ('draft', 'adopted')),
  constraint clause_lang_authenticity_zh_ck
    check (lang_authenticity_zh in ('OFFICIAL_TRANSLATION',
                                    'REVIEWED_WORKING_TRANSLATION',
                                    'MACHINE_TRANSLATION_UNVERIFIED')),
  -- K2 采纳纪律：模型只提候选，正式断言必须由具名人员采纳（红线第 2 条）。
  -- adopted 必须同时具名并带采纳时间，否则不构成「具名采纳」。
  constraint clause_adopted_requires_signature_ck
    check (status <> 'adopted' or (adopted_by is not null and adopted_at is not null)),
  -- 非署名断言必须有作准文本，等价于交接包 DDL 的 not null
  constraint clause_text_en_required_ck
    check (is_inference or text_en is not null),
  -- assertion_body 只属于署名断言（交接包 第二节：仅 is_inference 时非空）
  constraint clause_assertion_body_only_for_inference_ck
    check (assertion_body is null or is_inference)
);

comment on table clause is
  'K2 条款单元。只有 status = ''adopted'' 且来源 rights_status = ''ALLOW'' 的条款可被检索（交接包 第二节三条使用约束）。';
comment on column clause.id is
  '稳定标识，形如 ppwr/art/5/5/c，跨版本不变——可回链的前提（执行规划 T1 契约）。';
comment on column clause.scope is
  'all_packaging / food_contact_only / transport_only / not_product_scoped。最容易填错的一处：5(4) 为 all_packaging，5(5) 为 food_contact_only，填错会导致「非食品接触包装要不要测 PFAS」答错。not_product_scoped 指第 5(2)(3)(7)(8)(9) 条这类对委员会与成员国的义务——用户问「这条管不管我」时可直接据此回答，不必走越界拦截。';
comment on column clause.pending_dependency is
  '未出台依赖：挂钩哪份法案、有无法定截止日、在此之前按什么执行（规范 6.3 出口纪律第三条）。第 5(7)(8) 条的 statutory_deadline 为 null——条文是 may adopt，只能说第 64(2) 条的授权期限。';
comment on column clause.notes is
  '答题纪律，不是条文内容。随条款进模型上下文，但**不进 /api/clause/{id}**（交接包 第三节、第五节）。系统提示词须声明：据它调整取舍与措辞，不得引用、不得改写进答案正文。主题词召回不索引本列。';
comment on column clause.text_en_override is
  '提取件有缺陷时的人工原文。全批仅 PFAS 定义段一处——下标在提取件里被拆成独立行，按行号拼出的英文会带 3 2 这类残行。装载时有本列则优先于按行号取的文本。';
comment on column clause.assertion_body is
  '署名断言正文 {question_zh, coverage_check, reasoning_zh, output_requirements}。coverage_check 是「官方未覆盖」的举证记录，output_requirements 是输出约束。仅 is_inference = true 时非空。';
comment on column clause.bbox is
  '行级高亮框数组 [{page, x0, y0, x1, y1}]，归一化 0..1、原点页面左上。跨多行按行拆成多个矩形，不合并成一个大包围盒。';
comment on column clause.line_from is
  '_text_clean/<key>.txt 的 1 基行号。行号被规范、执行规划与本字段引用，不得漂移。';
comment on column clause.is_inference is
  'true 表示官方文件未覆盖、由专家署名推理得出，输出时必须标注为推理而非依据（红线第 6 条）。';
comment on column clause.numeric_limits is
  '[{name, value, unit, basis}]。注意 50 mg/kg 总氟是举证触发点，不是限值——basis 字段承担这个区分。';

create index if not exists clause_source_idx    on clause (source_id);
create index if not exists clause_path_idx      on clause (path_article, path_paragraph, path_point);
create index if not exists clause_annex_idx     on clause (annex) where annex is not null;
-- 检索只走已采纳条款，部分索引直接把草稿排除在外
create index if not exists clause_adopted_idx   on clause (source_id, path_article) where status = 'adopted';
create index if not exists clause_effective_idx on clause using gin (effective_expr);
create index if not exists clause_limits_idx    on clause using gin (numeric_limits);

-- ---------------------------------------------------------------------------
-- 条款间关系
-- ---------------------------------------------------------------------------
create table if not exists clause_link (
  from_clause_id text not null references clause(id),
  to_clause_id   text not null references clause(id),
  link_type      text not null,   -- requires / derogates / explains / amends / depends_on_pending
  note           text,

  constraint clause_link_type_ck
    check (link_type in ('requires', 'derogates', 'explains', 'amends', 'depends_on_pending')),
  constraint clause_link_pk
    primary key (from_clause_id, to_clause_id, link_type)
);

comment on table clause_link is
  '条款间关系。derogates 承载「减损与例外是规则库一等公民」（决策 #13）；depends_on_pending 承载「挂钩哪份尚未出台的法案」（规范 6.3 出口纪律第三条）。';

create index if not exists clause_link_to_idx on clause_link (to_clause_id);

-- ---------------------------------------------------------------------------
-- 已知冲突，由配置维护，新增无需改代码（决策 #4）
-- ---------------------------------------------------------------------------
create table if not exists conflict (
  id               text primary key,
  clause_ids       text[] not null,          -- 本处冲突涉及的全部条款，用于展示与回链
  summary_zh       text not null,
  resolution_zh    text not null,            -- 位阶指向哪一份。展示但不自动应用，不得据此跳过阻断
  authority_basis  text not null,            -- 位阶依据，接口要带出，免责文案要用
  trigger_clause_ids    text[] not null,     -- 判定阻断的依据，是 clause_ids 的子集
  trigger_context_terms text[],              -- 可选的第二条件，留空表示条款命中即触发
  trigger_hint          text,                -- 给人看的自由文本，**不参与判定**

  constraint conflict_clause_ids_ck check (array_length(clause_ids, 1) >= 2),
  constraint conflict_trigger_not_empty_ck check (array_length(trigger_clause_ids, 1) >= 1),
  -- 触发集必须是 clause_ids 的子集（conflicts.yaml C2 的 trigger_reason 明确要求）
  constraint conflict_trigger_subset_ck check (trigger_clause_ids <@ clause_ids)
);

comment on table conflict is
  '已知冲突登记。决策 #5：默认**暴露并阻断**，不自动择一——命中即只返回冲突说明、next_action = ack_conflict，不做答案合成也不调模型；用户确认后仍按位阶作答，不提供「按另一份作答」的选项（交接包 第三节冲突阻断流程）。';
comment on column conflict.resolution_zh is
  '位阶指向（如「以法规第 71 条为准」）。必须显示——PPWR 的规矩是冲突时按位阶且要在答案里说明；但**不得拿它跳过阻断直接作答**，那就变回自动择一了。';
comment on column conflict.trigger_clause_ids is
  '阻断判定的唯一依据：trigger_clause_ids ∩ 直接命中集 ≠ ∅。不可用 clause_ids 代替——C3 的 clause_ids 含第 5(4) 条，按它判定 14 条黄金用例误判 4 条。属本列的条款**不得由链接扩展带入命中集**，只能直接命中才算。';
comment on column conflict.trigger_context_terms is
  '触发的第二个条件，留空表示条款命中即触发。只有 C3 配了——FAQ 问 21 的 text_en 含 heavy metals 与 100 mg/kg（作准文本删不得），不加会让 G01/G06/G10 被误阻断。C1/C2 必须留空，加了反而漏触发。';
comment on column conflict.trigger_hint is
  '给人看的自由文本，**不得拿它做规则匹配**。判定一律走 trigger_clause_ids。';

create index if not exists conflict_clause_ids_idx   on conflict using gin (clause_ids);
create index if not exists conflict_trigger_ids_idx  on conflict using gin (trigger_clause_ids);

-- ---------------------------------------------------------------------------
-- 埋点（交接包 第七节）
-- ---------------------------------------------------------------------------
create table if not exists event (
  id           bigserial primary key,
  session_id   text not null,
  kind         text not null,   -- page_view / ask / citation_open /
                                -- intercept_l1 / intercept_l2 / goto_diagnosis /
                                -- lead_submit / no_citation / not_covered /
                                -- conflict_blocked / conflict_acked
  clause_id    text,
  input_tokens int,             -- 成本闸门监控
  latency_ms   int,
  meta         jsonb,
  created_at   timestamptz not null default now()
);

comment on table event is
  '埋点单表。执行规划 D1 的 9 项指标全部由此表 SQL 聚合。最需盯：intercept_l1 → goto_diagnosis 转化率（H24）与 input_tokens 分布（10K 成本闸门）。conflict_blocked 与 conflict_acked 之差是被冲突劝退的比例。**阻断态不得记 no_citation**，否则「无引用回答率为 0」这项指标被污染。';

create index if not exists event_kind_time_idx on event (kind, created_at desc);
create index if not exists event_session_idx   on event (session_id, created_at);
-- 按会话解锁图谱节点（D14）要按 session 查已引用过的条款
create index if not exists event_session_clause_idx
  on event (session_id, clause_id) where clause_id is not null;

-- ── 限流与预算（2026-09-06 拍板 D13）────────────────────────────────────
--
-- **计数存 PostgreSQL，不引入 Redis**：展示版量级下「能少一个组件就少一个」成立。
-- 用日期而不是滑动窗口——每 IP 每日 5 次，自然日归零，说得清也查得出。

create table if not exists rate_limit (
  ip     text  not null,
  day    date  not null,
  n      int   not null default 0,
  primary key (ip, day)
);

comment on table rate_limit is
  '每 IP 每日问答计数（D13：每日 5 次）。理由已由「防止一天打光整月额度」变为**纯粹的滥用防护**——火山单问成本降到 ¥0.01–0.08 后额度不再是保命项。它同时是 D14「按会话解锁」成立的前提：限流在，枚举攻击面才没有变大。**阈值按每天约 ¥20 估，充值额度定下来后须复核。**';

-- 月度花费。**按金额计，不按 token 计**——原定 100 万 token 是按 Opus 5
-- 的成本直觉定的，换火山定价后与真实成本脱钩（同样 100 万 token 在 2.1-pro 上只花 ¥10.7）。
create table if not exists spend (
  month  text  not null,          -- 'YYYY-MM'
  model  text  not null,
  calls  int   not null default 0,
  in_tok bigint not null default 0,
  out_tok bigint not null default 0,
  cny    numeric(12, 4) not null default 0,
  primary key (month, model)
);

comment on table spend is
  '月度花费按金额累计（D13）。两级、**不硬停**：70% 告警，100% 触发超限降级（只出两段式第一段，不调模型）。不硬停的理由是降级态「比没上线更伤」；但降级不等于白屏——条款清单仍有料。';

commit;
