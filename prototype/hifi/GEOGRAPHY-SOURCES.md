---
feature_ids: []
topics: [prototype, geography, natural-earth, ukraine]
doc_kind: reference
created: 2026-09-07
---

# 离线地图底图来源

## 内容与用途

`geography.svg.fragment` 是供本次单文件 HTML 原型内嵌的 SVG 片段，不是单独可打开的完整 SVG。它包含 22 个国家路径和 13 段主要河流路径，完全离线，不依赖地图瓦片、图片或运行时网络请求。底图不包含当前控制线、前线、部署、道路或自行绘制的行政区划。

国家采用 Natural Earth 的 **Ukraine POV** 国界版本；乌克兰形状包含克里米亚。本图表达国界，不表达某地当下由谁控制。不能用此静态底图推断战场态势。

## 原始资料

- [Natural Earth 1:10m Countries — Ukraine POV GeoJSON](https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries_ukr.geojson)
- [Natural Earth 1:10m Rivers and Lake Centerlines GeoJSON](https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_rivers_lake_centerlines.geojson)
- [Natural Earth 的不同国界观点说明](https://www.naturalearthdata.com/blog/admin-0-countries-point-of-views/)
- [Natural Earth 使用条款](https://www.naturalearthdata.com/about/terms-of-use/)

获取日期为 2026-09-07（Europe/Kiev）。上游链接指向 `master`，内容可能在未来更新；本次交付使用生成时已内嵌的静态路径，不会自动更新。Natural Earth 地图数据为 public domain，可修改和分发；原型仍建议保留小型 “Natural Earth” 来源链接。

## 几何处理

地图范围为 18°E—46°E、41°N—55°N，使用约定的线性经纬度屏幕映射：

```js
x = (longitude - 18) * 1400 / 28;
y = (55 - latitude) * 900 / 14;
```

目标 `viewBox="0 0 1400 900"`。该显示映射不保角、不适合精确测距，不能据此标注固定比例尺。国家轮廓裁切到带 2 px 余量的视口，随后以约 0.62 px 容差简化；河流以约 0.45 px 容差简化。原始资料本身为小比例尺概化数据，不适用于街道级定位。

河流包含 Dnipro / Dnepre、Dniester、Danube、Don、Pripyat、Donets、Southern Bug；保留原始名称，同一条河可能由多段独立路径组成。只取原始数据，没有手画替代线。河流和水系形态不是实时水文记录。

## 接入方式

把片段内容直接插入主 HTML 的 SVG 地理图层。国家路径使用 `class="country"`，乌克兰额外带 `ukraine`；河流使用 `class="river"`。各路径有 `data-name`。由主页面统一设置填充、描边、透明度等，建议示例：

```css
.country { fill: #171e22; stroke: #344047; stroke-width: 0.75; }
.country.ukraine { fill: #20292c; stroke: #718074; stroke-width: 1.1; }
.river { fill: none; stroke: #354b55; stroke-width: 1.1; }
```

外层 SVG 应裁切至自身视口；河流路径有少量视口外坐标。若实现缩放和平移，所有地理路径、城市标签、事件点必须放在同一变换组内。事件点坐标应使用同一映射函数，不能按目测定位。

## 轻量核查

- 片段含 22 个国家路径、13 个河流路径，无 NaN / Infinity 坐标。
- 对简化后乌克兰路径作点内判断，Kyiv、Simferopol、Sevastopol、Donetsk 位于路径内，Moscow 不在路径内。
- 这些核查只验证片段结构与若干关键包含关系，不替代全面 GIS 拓扑审查，也不证明其为最新边界或实时态势数据。
