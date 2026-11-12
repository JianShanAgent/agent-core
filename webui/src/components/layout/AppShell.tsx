// 整体框架：左边侧栏分栏目，右边是页面；最上面是和配置状态有关的提示条。

import {
  Activity,
  BookOpen,
  Cpu,
  Globe,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  MessagesSquare,
  Monitor,
  Moon,
  Puzzle,
  ScrollText,
  Server,
  Settings,
  Sun,
  Users,
  Waypoints,
  X,
} from "lucide-react";
import { type ComponentType, type ReactNode, Suspense, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router";

import { LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Hint } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatUptime } from "@/lib/format";
import { type Theme, useStatus, useTheme } from "@/lib/hooks";
import type { Status } from "@/lib/types";
import { cn } from "@/lib/utils";

import { StateBanners } from "./StateBanners";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  end?: boolean;
}

const NAV: { title?: string; items: NavItem[] }[] = [
  {
    items: [
      { to: "/", label: "首页", icon: LayoutDashboard, end: true },
      { to: "/chat", label: "网页对话", icon: MessageSquare },
    ],
  },
  {
    title: "观察",
    items: [
      { to: "/traces", label: "查证过程", icon: Waypoints },
      { to: "/calls", label: "模型调用", icon: Activity },
      { to: "/logs", label: "实时日志", icon: ScrollText },
    ],
  },
  {
    title: "配置",
    items: [
      { to: "/config", label: "机器人配置", icon: Settings },
      { to: "/models", label: "模型管理", icon: Cpu },
      { to: "/plugins", label: "插件", icon: Puzzle },
      { to: "/wikis", label: "Wiki", icon: Globe },
    ],
  },
  {
    title: "数据",
    items: [
      { to: "/knowledge", label: "知识库", icon: BookOpen },
      { to: "/sessions", label: "用户与会话", icon: Users },
      { to: "/chatlog", label: "群聊记录", icon: MessagesSquare },
    ],
  },
  { title: "系统", items: [{ to: "/system", label: "系统信息", icon: Server }] },
];

const THEMES: { value: Theme; label: string; icon: ComponentType<{ className?: string }> }[] = [
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
  { value: "system", label: "跟随系统", icon: Monitor },
];

/** 机器人和 QQ 的连接情况：一个圆点加一句话。 */
function connection(status: Status | undefined, offline: boolean): { dot: string; text: string } {
  if (offline || !status) return { dot: "bg-destructive", text: "连不上机器人" };
  if (status.dry_run) return { dot: "bg-warning", text: "调试模式，没有连 QQ" };
  if (status.gateway?.connected) return { dot: "bg-success", text: "QQ 已连接" };
  return { dot: "bg-destructive", text: "QQ 连接断开" };
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <img src="/favicon.svg" alt="" className="size-8 rounded-lg" />
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">JianShanAgent</div>
        <div className="text-muted-foreground text-[11px]">Wiki 机器人 · 管理后台</div>
      </div>
    </div>
  );
}

function Sidebar({ onNavigate, onLogout }: { onNavigate?: () => void; onLogout: () => void }) {
  const { theme, setTheme } = useTheme();
  const { data: status, isError } = useStatus();
  const link = connection(status, isError);
  return (
    <div className="bg-sidebar text-sidebar-foreground flex h-full w-60 flex-col border-r border-sidebar-border">
      <div className="px-3 pt-4 pb-3">
        <Brand />
      </div>
      <nav className="flex-1 overflow-y-auto px-3 pb-3">
        {NAV.map((group, index) => (
          <div key={group.title ?? index} className="mt-3 first:mt-0">
            {group.title ? (
              <div className="text-muted-foreground/80 px-2 pb-1 text-[11px] font-medium tracking-wider">
                {group.title}
              </div>
            ) : null}
            <div className="flex flex-col gap-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors",
                      isActive
                        ? "bg-sidebar-accent text-foreground font-medium"
                        : "hover:bg-sidebar-accent/60 hover:text-foreground",
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <item.icon className={cn("size-4", isActive ? "text-brand" : "opacity-70")} />
                      {item.label}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
      <div className="border-t border-sidebar-border px-3 py-3">
        <div className="flex items-center gap-2 px-2 text-xs">
          <span className={cn("size-2 shrink-0 rounded-full", link.dot)} />
          <span className="truncate">{link.text}</span>
        </div>
        {status ? (
          <div className="text-muted-foreground mt-1 px-2 text-[11px] leading-relaxed">
            已运行 {formatUptime(status.uptime_seconds)}
            {status.release ? ` · 发布 ${status.release}` : ` · v${status.version}`}
            {status.update?.available ? (
              <Link to="/system" className="text-brand ml-1 font-medium hover:underline">
                有新版本 v{status.update.latest}
              </Link>
            ) : null}
          </div>
        ) : null}
        <div className="mt-2.5 flex items-center justify-between">
          <div className="bg-sidebar-accent/70 flex items-center gap-0.5 rounded-md p-0.5">
            {THEMES.map((item) => (
              <Hint key={item.value} text={item.label}>
                <button
                  type="button"
                  onClick={() => setTheme(item.value)}
                  className={cn(
                    "flex size-6 cursor-pointer items-center justify-center rounded-[5px] transition-colors",
                    theme === item.value
                      ? "bg-card text-foreground shadow-xs"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <item.icon className="size-3.5" />
                  <span className="sr-only">{item.label}</span>
                </button>
              </Hint>
            ))}
          </div>
          <Hint text="退出登录">
            <Button variant="ghost" size="icon-xs" onClick={onLogout}>
              <LogOut />
              <span className="sr-only">退出登录</span>
            </Button>
          </Hint>
        </div>
      </div>
    </div>
  );
}

export function AppShell({ onLogout }: { onLogout: () => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMenuOpen(false), [location.pathname]);
  const logout = () => {
    void api.logout().finally(onLogout);
  };
  return (
    <div className="flex h-dvh overflow-hidden">
      <aside className="hidden lg:block">
        <Sidebar onLogout={logout} />
      </aside>
      {menuOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/45" onClick={() => setMenuOpen(false)} />
          <div className="absolute inset-y-0 left-0 shadow-xl">
            <Sidebar onNavigate={() => setMenuOpen(false)} onLogout={logout} />
          </div>
          <Button
            variant="secondary"
            size="icon-sm"
            className="absolute top-3 left-[15.5rem]"
            onClick={() => setMenuOpen(false)}
          >
            <X />
          </Button>
        </div>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="bg-background/90 flex items-center gap-2 border-b px-3 py-2 backdrop-blur lg:hidden">
          <Button variant="ghost" size="icon-sm" onClick={() => setMenuOpen(true)}>
            <Menu />
            <span className="sr-only">菜单</span>
          </Button>
          <span className="text-sm font-semibold">JianShanAgent 管理后台</span>
        </header>
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1400px] px-4 py-5 sm:px-6 lg:px-8">
            <StateBanners />
            <Suspense fallback={<LoadingRows className="mx-auto max-w-3xl pt-16" />}>
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
    </div>
  );
}

/** 占满主区域高度的页面（网页对话、日志）用：自己管滚动。 */
export function FullHeight({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex h-[calc(100dvh-2.5rem)] min-h-[28rem] flex-col lg:h-[calc(100dvh-2.5rem)]", className)}>
      {children}
    </div>
  );
}
