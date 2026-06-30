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

// 字幕交付方式（M2.1 起全部可选）：独立 SRT / 烧录进画面 / 两者都要。烧录由内核 libass 重编码实现；
// 后端按 output_mode + subtitle_delivery 校验交付物，烧录视频复用 video_key（不新增字段）。
export const SUBTITLE_DELIVERY_OPTIONS: DeliveryOption[] = [
  { value: "srt", label: "独立字幕文件（SRT）", disabled: false },
  { value: "burned", label: "烧录进画面", disabled: false },
  { value: "both", label: "SRT + 烧录进画面", disabled: false },
];
