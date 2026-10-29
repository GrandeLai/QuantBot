import { type ComponentType } from "react";
import {
  Activity,
  BookOpen,
  FlaskConical,
  PlayCircle,
  ShieldAlert,
} from "lucide-react";

export type WorkbenchTab =
  | "research"
  | "strategy"
  | "validation"
  | "run"
  | "risk_review";

export interface WorkbenchTabDef {
  key: WorkbenchTab;
  label: string;
  icon: ComponentType<{ size?: number }>;
}

export const WORKBENCH_TABS = [
  { key: "research", label: "研究中心", icon: Activity },
  { key: "strategy", label: "策略库", icon: BookOpen },
  { key: "validation", label: "验证中心", icon: FlaskConical },
  { key: "run", label: "运行中心", icon: PlayCircle },
  { key: "risk_review", label: "风险与复盘", icon: ShieldAlert },
] as const satisfies readonly WorkbenchTabDef[];
