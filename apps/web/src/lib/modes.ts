import type { OutputMode, SubtitleDelivery } from "./types";

export interface ModeOption {
  value: OutputMode;
  label: string;
  hint: string;
}

// 输出模式选择器（验收）：字幕 / 配音 / 双语。
export const OUTPUT_MODE_OPTIONS: ModeOption[] = [
  { value: "subtitle_only", label: "仅字幕", hint: "生成翻译字幕（SRT），最长约 30 分钟" },
  { value: "dub_only", label: "配音", hint: "生成配音音轨，最长约 5 分钟" },
  { value: "both", label: "字幕 + 配音", hint: "字幕与配音都生成，最长约 5 分钟" },
];

export interface DeliveryOption {
  value: SubtitleDelivery;
  label: string;
  disabled: boolean;
}

// 烧录（burned-in）字幕属 M2.1，前端先 disable 并标「即将支持」（CodeX P2.6）。后端也只接受 srt：
// createJob 对非 srt 返 unsupported_subtitle_delivery，所以 disabled 选项即便被绕过也会被服务器拒。
export const SUBTITLE_DELIVERY_OPTIONS: DeliveryOption[] = [
  { value: "srt", label: "独立字幕文件（SRT）", disabled: false },
  { value: "burned", label: "烧录进画面（即将支持）", disabled: true },
];
