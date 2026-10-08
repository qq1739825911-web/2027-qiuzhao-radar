# 2027秋招雷达 V1.3 架构

## 目标
把招聘入口收藏页升级成可维护的全专业招聘数据产品。

## 数据流
官方/公开信源 → source registry → collector → inbox → normalize → dedupe → expiry → validate → jobs.json → GitHub Pages

## 核心原则
1. 每条真实岗位保留原始来源 URL。
2. 不把搜索结果摘要当成招聘事实。
3. 不绕过登录、验证码、反爬或私有 API。
4. 数据字段缺失时保留为空，不伪造截止日期、薪资或学历要求。
5. 多信源确认使用 confirmed_by 记录来源，不直接复制第三方数据集。
6. GitHub Actions 负责重复运行、质量检查和过期处理。

## V1.3 已完成
- 全新数据状态提示与最近核验时间。
- 接入首批官方可核验岗位样例：华为 2027 届校园招聘公开职位。
- data/company_sources.json：允许采集的信源白名单。
- scripts/collect_public_sources.py：公开信源采集框架。
- docs/research-and-licenses.md：第三方项目研究与许可证记录。

## 下一阶段
- 为不同公开信源实现 source-specific parser。
- 建立 data/inbox/ 原始采集区。
- 增加职位详情字段：薪资、专业要求、职位描述、岗位要求、申请链接。
- 增加指纹去重和跨信源确认。
- V1.4 加入本地求职画像匹配分数。
