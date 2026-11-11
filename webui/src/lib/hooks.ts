import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { api } from "./api";

// ---- 主题 ----

export type Theme = "light" | "dark" | "system";
const THEME_KEY = "jianshanagent.theme";
const listeners = new Set<() => void>();

function readTheme(): Theme {
  const saved = localStorage.getItem(THEME_KEY);
  return saved === "light" || saved === "dark" ? saved : "system";
}

const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;

/** 主题变了（在页面上切换，或者系统的深浅色变了）都通知一声。 */
function subscribeTheme(notify: () => void): () => void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  listeners.add(notify);
  media.addEventListener("change", notify);
  return () => {
    listeners.delete(notify);
    media.removeEventListener("change", notify);
  };
}

/** `dark`：现在实际是不是深色（选了「跟随系统」时看系统）。图表的颜色要跟着它变。 */
export function useTheme(): { theme: Theme; setTheme: (theme: Theme) => void; dark: boolean } {
  const theme = useSyncExternalStore(subscribeTheme, readTheme);
  const system = useSyncExternalStore(subscribeTheme, systemDark);
  const dark = theme === "dark" || (theme === "system" && system);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);
  const setTheme = useCallback((next: Theme) => {
    localStorage.setItem(THEME_KEY, next);
    for (const notify of [...listeners]) notify();
  }, []);
  return { theme, setTheme, dark };
}

// ---- 机器人的现状（侧栏、提示条用；每 10 秒问一次） ----

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 10_000 });
}

/** 改了配置、插件之后：让所有跟配置有关的数据重新取。 */
export function useRefreshConfig(): () => void {
  const client = useQueryClient();
  return useCallback(() => {
    for (const key of ["status", "config", "models", "plugins", "overview", "system", "kb"]) {
      void client.invalidateQueries({ queryKey: [key] });
    }
  }, [client]);
}

// ---- 记在浏览器里的小设置 ----

export function useStored<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const update = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* 存不下（比如带图片的对话太大）：只留在内存里 */
      }
    },
    [key],
  );
  return [value, update];
}

/** 输入停下来一会儿之后才生效的值（搜索框用，免得每敲一个字查一次）。 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
