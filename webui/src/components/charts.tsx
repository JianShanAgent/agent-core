// 图表的共用部分：配色、提示框、环形图。颜色都取自主题变量，亮暗主题自动跟着变。

import type { ReactNode } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

export const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

export const AXIS_TICK = { fill: "var(--muted-foreground)", fontSize: 11 };
export const GRID_STROKE = "var(--border)";

export interface TipProps {
  active?: boolean;
  label?: string | number;
  payload?: readonly {
    name?: string | number;
    value?: unknown;
    color?: string;
    dataKey?: unknown;
  }[];
}

/** 鼠标停在图上时的提示框。`format` 决定每个数怎么显示。 */
export function ChartTip({
  active,
  label,
  payload,
  title,
  format = (value) => formatNumber(Number(value)),
}: TipProps & {
  title?: (label: string) => ReactNode;
  format?: (value: unknown, key: string) => ReactNode;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-popover text-popover-foreground min-w-32 rounded-lg border px-3 py-2 text-xs shadow-md">
      {label !== undefined ? (
        <div className="text-muted-foreground mb-1">{title ? title(String(label)) : label}</div>
      ) : null}
      <div className="flex flex-col gap-1">
        {payload.map((item) => (
          <div key={String(item.dataKey ?? item.name)} className="flex items-center gap-2">
            <span className="size-2 shrink-0 rounded-[2px]" style={{ background: item.color }} />
            <span className="flex-1">{item.name}</span>
            <span className="font-medium tabular-nums">
              {format(item.value, String(item.dataKey ?? item.name))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 环形图 + 右边的图例（名字、数量、占比）。 */
export function Donut({
  data,
  height = 176,
  className,
}: {
  data: { name: string; value: number }[];
  height?: number;
  className?: string;
}) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  const shown = data.filter((item) => item.value > 0);
  return (
    <div className={cn("flex items-center gap-4", className)}>
      <div className="shrink-0" style={{ width: height, height }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={shown}
              dataKey="value"
              nameKey="name"
              innerRadius="62%"
              outerRadius="92%"
              paddingAngle={shown.length > 1 ? 2 : 0}
              stroke="none"
              isAnimationActive={false}
            >
              {shown.map((item, index) => (
                <Cell key={item.name} fill={CHART_COLORS[index % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip content={(props) => <ChartTip {...(props as TipProps)} />} />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 text-xs">
        {shown.map((item, index) => (
          <div key={item.name} className="flex items-center gap-2">
            <span
              className="size-2.5 shrink-0 rounded-[3px]"
              style={{ background: CHART_COLORS[index % CHART_COLORS.length] }}
            />
            <span className="min-w-0 flex-1 truncate">{item.name}</span>
            <span className="tabular-nums">{formatNumber(item.value)}</span>
            <span className="text-muted-foreground w-11 text-right tabular-nums">
              {formatPercent(item.value, total)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
