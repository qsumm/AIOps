// wn_pixelscene.js —— 更新公告右栏的像素插画（本地像素画资产 + Canvas 最近邻放大）
//
// 这条路的演变（三轮被否之后定下来的，别再走回头路）：
//   D130 玻璃拟态 + 高斯发光 + 假 KPI              → 用户判「太AI了」
//   D131 纯 CSS/SVG 信号带（读数带）                → 干净但缺叙事，像图表不像人
//   D132 图像模型生成的套印海报                    → 用户判「丑死了」，整体回退
//   D133 逐格手绘像素画（代码画、纯色、灰白）      → 用户判「有点丑」：像素工艺不到位
//   D134（本版）**16-bit RPG 级像素场景**：
//        画面由图像模型生成后经**像素化后处理**（降采样到 160×90 硬网格 + 24 色调色板 +
//        Floyd–Steinberg 抖动）转成真正的像素画——网格纪律、抖动颗粒、明度多阶都齐了；
//        资产**入库为本地 PNG**（static/img/whatsnew-scene.png，4.5KB），
//        页面用最近邻整数倍放大绘制 → 边缘硬朗、离线可用、无外链、无 CDN。
//
// 纪律（由 test_d130_whatsnew.py 机械守卫）：
//   · 只引用**本地**像素画资产（禁 http/https、禁 base64 内嵌、禁 CDN）
//   · 资产必须是低分辨率像素网格（≤256px）且体积受限（≤64KB）
//   · 右栏不得出现渐变 / 模糊 / 发光 / 投影 / 假读数
//   · 若将来加动画，必须尊重 prefers-reduced-motion
//
// 渲染：132×176 竖构图资产 → 2 倍最近邻放大到 264×352 画布（整数倍，硬边不糊）。

const DEFAULT_SRC = "/img/whatsnew-scene.png";
const SCALE = 2;                 // 整数倍放大：像素画必须整数倍，否则边缘发虚
const CELL = 132;                // 资产宽度（像素格数）
const ROWS = 176;                // 资产高度（像素格数，竖构图——填满右栏，不留大片空

/**
 * 挂载像素画。
 * @param {{src?: string}} opts src 可指定其它本地资产（设计预览页用相对路径）
 * @returns {{el: HTMLElement, destroy: () => void}}
 */
export function mountPixelScene(opts = {}) {
  const src = opts.src || DEFAULT_SRC;
  const cv = document.createElement("canvas");
  cv.width = CELL * SCALE;
  cv.height = ROWS * SCALE;
  cv.style.width = CELL * SCALE + "px";
  cv.style.height = ROWS * SCALE + "px";
  cv.style.imageRendering = "pixelated";   // 最近邻：放大后仍是一个个方块
  cv.setAttribute("role", "img");
  cv.setAttribute("aria-label", "插画：像素风格的运维工程师与猫在双屏前值守");

  const ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false;

  const el = document.createElement("div");
  el.className = "wn2-scene";
  el.append(cv);

  const img = new Image();
  img.onload = () => {
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, 0, 0, cv.width, cv.height);
  };
  // 资产缺失/加载失败：留一块与画布同尺寸的底（不塌版、不出破图）
  img.onerror = () => {
    ctx.fillStyle = "#F2F3F5";
    ctx.fillRect(0, 0, cv.width, cv.height);
  };
  img.src = src;

  return { el, destroy() { /* 静态资产，无动画需停 */ } };
}
