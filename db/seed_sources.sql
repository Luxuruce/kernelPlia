-- K1 语料与权利登记 · V0 四份来源
--
-- 对应任务：V0_验收标准文档 该任务（K1 最小形态）
-- 元数据一律取自 knowledge/ppwr/EUoffical/_text_clean/ 的提取件正文，不凭记忆填写。
-- ELI 只登记原文中出现的自身 ELI：指南与 FAQ 正文未给出自身 ELI，留空，不编造。
--
-- rights_status：产品契约文档 第二节明确 V0 四份来源均为欧盟公开法源，登记后置为 ALLOW。
-- 字段与判断必须存在——第二簇接入标准类内容时默认 RIGHTS_BLOCKED（决策 #8）。

insert into source (id, title, instrument_class, authority_rank,
                    eli, oj_ref, version, lang_authenticity, rights_status, ingested_at)
values
  -- 位阶 1：法规正文与附件
  ('ppwr_reg',
   'Regulation (EU) 2025/40 of the European Parliament and of the Council of 19 December 2024 on packaging and packaging waste, amending Regulation (EU) 2019/1020 and Directive (EU) 2019/904, and repealing Directive 94/62/EC',
   'regulation', 1,
   'http://data.europa.eu/eli/reg/2025/40/oj',
   'OJ L series, 2025/40, 22.1.2025',
   '2025-01-22',
   'ORIGINAL_AUTHENTIC', 'ALLOW', now()),

  -- 位阶 2：授权法案。依 TFEU 第 290 条通过，可增设法规正文里没有的豁免——
  -- 只读法规正文就下结论是错的（规范 5.2）。本件即 11.3 节的位阶冲突实例。
  ('dec_2026_429',
   'Commission Delegated Decision (EU) 2026/429 of 25 February 2026 on supplementing Regulation (EU) 2025/40 by exempting certain economic operators that use pallet wrappings and straps from the 100 % reuse requirements of these packaging formats',
   'delegated_act', 2,
   'http://data.europa.eu/eli/dec_del/2026/429/oj',
   'OJ L series, 2026/429, 6.5.2026',
   '2026-05-06',
   'ORIGINAL_AUTHENTIC', 'ALLOW', now()),

  -- 位阶 3：官方实施指南。委员会解释，无法律约束力，但成员国与法院会重视。
  ('ppwr_guidance',
   'Commission Notice — Guidance document for Regulation (EU) 2025/40 on packaging and packaging waste (C/2026/3084)',
   'guidance', 3,
   null,
   'OJ C series, C/2026/3084, 10.6.2026',
   '2026-06-10',
   'ORIGINAL_AUTHENTIC', 'ALLOW', now()),

  -- 位阶 4：FAQ。委员会解释，无约束力，常明示「仅反映作者观点」。
  ('ppwr_faq',
   'Packaging and Packaging Waste Regulation (PPWR) — Frequently Asked Questions, DG ENV Unit B01, August 2026 (2nd edition)',
   'faq', 4,
   null,
   'DG ENV, Unit B01, August 2026',
   '2026-08',
   'ORIGINAL_AUTHENTIC', 'ALLOW', now())

on conflict (id) do nothing;
