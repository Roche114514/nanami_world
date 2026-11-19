# 七海千秋 · 文字世界（nanami_world v0.2.1）

面向 **Shinsekai（>= 2.6.0）** 的独立像素风自定义文字世界插件。

对大语言模型来说，世界由语言构成；对人类来说，前端是一个可高度自定义调色板的俯视像素小屋。你只需输入一句话即可同时编译出**规范化的智能物件属性**与**透明底像素贴图**，把真实存在于账本中的物品摆进离散坐标格网里，让角色在物质守恒与物理边界内自主感知与互动。

---

## v0.2.1 核心升级

1. **输入（L0 `PerceptFrame`）→ 思考（Brain）→ 输出（Mouth/Motor）三层解耦**：
   - **输入层**：`persona_bridge.build_percept_frame` 统一封装 L0 开关、Groves–Thompson 习惯化熟悉度、Tolman/ACT-R 心理地图与 Borbély 昼夜节律，并将实体编号与系统遥测过滤为纯感官描述。
   - **思考与输出隔离**：自主行动（含手动触发「自主行动」）、世界对话与多角色社交统一消费 `PerceptFrame`，彻底屏蔽内部 `entity_id` 与进度数值泄漏；前端状态页实时显示当前 L0 阶段、熟悉度与焦点物品。
2. **大脑–嘴巴双层解耦与原声语料驱动**：
   - 世界对话与双人自主社交全面接入 `nanami_persona` 的 `mind.mjs` 心理推演与 `corpus.tsv` 原声语料抽样，配合跨说话人防复读扣分，告别空泛 AI 腔。
3. **像素小人无损提取 & PixelLab 备用接入**：支持从角色图无损提取像素小人并接入 PixelLab 备用生成，结合坐标落格与 Y-sort 遮挡排序。
4. **全区可调 / 全屏画布与独立设置页（`web/settings.html`）**：画布支持全区尺寸调节与全屏展开，设置中心提供独立的自定义配置界面。
5. **多世界状态隔离与口语化行动文案**：每个世界独立隔离角色心境参数与社交指针，并统一简化各角色的自主行动内心提示文案。

---

## 核心特性

1. **「以文生像素」造物（零显存 / 零 VLM）**：输入一句话（如「桌角一杯冒着热气的抹茶」），复用宿主纯文本大模型一次产出智能物件规范与受限 7 色槽像素矩阵，并配备页内 16×16 像素微调画笔，也支持上传外部静态 PNG 立牌/贴图覆盖皮肤。
2. **严格物质守恒**：内置 10 件默认像素贴图仅作素材蓝图；只有被用户放进空间的物体才写入世界账本 `world.json`。消耗品归零即空或消失，闭合容器内的物品脱离角色视野，LLM 无法编造或操作账本外的道具。
3. **10 个正交智能物件组件 + 4 个统一工具**：`portable` / `surface` / `container` / `consumable` / `thermal` / `switchable` / `restable` / `activity` / `inscribable` / `portal` 自动推导出此刻合法动词，配合 `world_act` / `world_move` / `world_write` / `world_combine` 安全交互。
4. **明确离散坐标空间与静态角色导入**：支持一键扫描 `data/config/characters.yaml` 导入宿主角色或新建自定义角色，采用单帧静态贴图按脚底坐标 `(x, y)` 精确落格与 Y-sort 遮挡排序（不做复杂骨骼动画），配合 BFS 连通寻路与多空间门拓扑拓展。
5. **双视界切换与高度自定义调色板**：内置 5 套像素色板预设与色块/Hex 导入导出；点顶部「👁 她的视角」一键在像素小屋与复古文字终端间切换。
6. **两级离线心跳 & 与 `nanami_persona` 零耦合联动**：0-Token 物理时钟推进昼夜、降温与作息；若同时安装了 `nanami_persona`，自动只读感知 `mind.json` 与 `traits.json` 并导出 `bridge/<actor_id>.json`。

---

## 安装与启用

1. 从 Shinsekai 插件市场安装，或将仓库放入 `plugins/nanami_world/`。
2. 在 `data/config/plugins.yaml` 中启用：

```yaml
- entry: plugins.nanami_world.plugin:NanamiWorldPlugin
  enabled: true
```

重启 Shinsekai 后，可在聊天工具条点击 **「文字世界」** 按钮（或进入工具/设置页）打开画布。

---

## 数据隔离说明

所有用户创造的空间、蓝图、调色板、上传贴图与事件流均保存在独立数据目录 `data/plugins/community.nanami.world/` 下；升级或重新安装插件不会覆盖任何世界数据。
