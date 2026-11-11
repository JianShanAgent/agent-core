import "./index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { Toaster } from "sonner";

import { TooltipProvider } from "@/components/ui/misc";
import { ApiError } from "@/lib/api";
import { useTheme } from "@/lib/hooks";

import { App } from "./App";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: true,
      // 没登录、没有数据库、参数不对：再试也是一样的结果
      retry: (count, error) =>
        count < 1 && !(error instanceof ApiError && error.status >= 400 && error.status < 600),
    },
  },
});

function Toasts() {
  const { dark } = useTheme();
  return <Toaster theme={dark ? "dark" : "light"} position="top-center" richColors closeButton />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={250}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
        <Toasts />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
