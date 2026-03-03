# TODO

- [ ] 推断的更新：推断明显会因为一些新发生的事情变得提前失效，缺少复核（复核时机怎么设计？）
- [ ] SOUL.md更新尚未实现，目前实现了对USER.md的更新 
- [ ] cluster现在只是按照日期分桶
- [ ] cluster在检索的时候没有生效
- [ ] 预留的clusterSimilarityThreshold、clusterMaxTimeGapDays没有在代码中使用
- [ ] PROFILE_LIFE_UPDATE_PROMPT life profile更新的时候没有考虑cluster的信息，应该把聚合的相关信息都带出来供这次使用

# 问题记录

## 效果类问题
- [ ] 看起来聚类效果一般，聊了挺多都单独被聚类

## Bug

暂无

## 设计问题
- [ ] Memory.md的更新是由边界判断的时候LLM（CONV_BOUNDARY_DETECTION_PROMPT）生成的topic_summary，能够保证很短，但是是否和后面的场景和事件记忆一致不确定
    - 进一步的问题：这个可以作为中期记忆吗？如果可以超长压缩的prompt应该要考虑这个原则，先缩减中记忆