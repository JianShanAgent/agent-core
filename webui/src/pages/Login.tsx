import { KeyRound } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { api } from "@/lib/api";

export function Login({ onDone, locked }: { onDone: () => void; locked: number }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState(
    locked > 0 ? `输错的次数太多了，${locked} 秒后再试` : "",
  );
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.login(token.trim());
      onDone();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-dvh items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <img src="/favicon.svg" alt="" className="size-12 rounded-xl shadow-sm ring-1 ring-white/15" />
          <div>
            <h1 className="text-lg font-semibold tracking-tight">JianShanAgent 管理后台</h1>
            <p className="text-muted-foreground mt-1 text-sm">Wiki QQ 机器人</p>
          </div>
        </div>
        <div className="bg-card rounded-xl border p-5 shadow-sm">
          <label className="text-sm font-medium" htmlFor="token">
            登录令牌
          </label>
          <div className="relative mt-2">
            <KeyRound className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
            <Input
              id="token"
              type="password"
              autoFocus
              autoComplete="current-password"
              placeholder="粘贴令牌"
              className="pl-9"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              aria-invalid={Boolean(error)}
            />
          </div>
          {error ? <p className="text-destructive mt-2 text-sm">{error}</p> : null}
          <Button type="submit" className="mt-4 w-full" loading={busy} disabled={!token.trim()}>
            登录
          </Button>
        </div>
        <p className="text-muted-foreground mt-4 text-center text-xs leading-relaxed">
          令牌在 <code className="font-mono">secrets.yaml</code> 的{" "}
          <code className="font-mono">admin.token</code>；没填的话，第一次启动时自动生成在{" "}
          <code className="font-mono">data/admin/token</code>（Docker 容器里是挂载数据目录下的{" "}
          <code className="font-mono">bot/admin/token</code>）。
        </p>
      </form>
    </div>
  );
}
