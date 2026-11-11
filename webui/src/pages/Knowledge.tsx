// 知识库：机器人查证时第一步查的资料。概况、翻切片、试着检索一次、翻图库、以图搜图。

import { BookOpen, Images, LayoutList, ScanSearch, Search } from "lucide-react";
import { type ComponentType, useState } from "react";
import { useSearchParams } from "react-router";

import { PageHeader } from "@/components/common";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { Chunks } from "./knowledge/Chunks";
import { Gallery } from "./knowledge/Gallery";
import { ImageSearch } from "./knowledge/ImageSearch";
import { Overview } from "./knowledge/Overview";
import { SearchTry } from "./knowledge/SearchTry";

type TabName = "overview" | "chunks" | "search" | "images" | "image-search";

const TABS: { value: TabName; label: string; icon: ComponentType<{ className?: string }> }[] = [
  { value: "overview", label: "概况", icon: BookOpen },
  { value: "chunks", label: "切片", icon: LayoutList },
  { value: "search", label: "试检索", icon: Search },
  { value: "images", label: "图库", icon: Images },
  { value: "image-search", label: "以图搜图", icon: ScanSearch },
];

export default function Knowledge() {
  const [params, setParams] = useSearchParams();
  const wanted = params.get("tab");
  const tab = TABS.find((item) => item.value === wanted)?.value ?? "overview";
  // 看过的标签切走以后不卸载：搜到一半的词、翻到的页、试检索的结果回来时都还在。
  // 没看过的不提前装，免得一进来就把五个标签的数据全取一遍。
  const [visited, setVisited] = useState<TabName[]>([tab]);
  if (!visited.includes(tab)) setVisited([...visited, tab]);

  const open = (next: TabName) => {
    const copy = new URLSearchParams(params);
    if (next === "overview") copy.delete("tab");
    else copy.set("tab", next);
    setParams(copy, { replace: true });
  };
  const panel = (name: TabName) => visited.includes(name) || tab === name;

  return (
    <div>
      <PageHeader
        title="知识库"
        description="机器人查证时第一步查的资料：wiki 切成的文本切片，和用来认图的图库。可以翻，也可以照机器人的办法查一次，看它会捞到什么。"
      />
      <Tabs value={tab} onValueChange={(next) => open(next as TabName)}>
        <TabsList>
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value}>
              <item.icon /> {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {panel("overview") ? (
          <TabsContent value="overview" forceMount className="data-[state=inactive]:hidden">
            <Overview onOpenTab={open} />
          </TabsContent>
        ) : null}
        {panel("chunks") ? (
          <TabsContent value="chunks" forceMount className="data-[state=inactive]:hidden">
            <Chunks />
          </TabsContent>
        ) : null}
        {panel("search") ? (
          <TabsContent value="search" forceMount className="data-[state=inactive]:hidden">
            <SearchTry />
          </TabsContent>
        ) : null}
        {panel("images") ? (
          <TabsContent value="images" forceMount className="data-[state=inactive]:hidden">
            <Gallery />
          </TabsContent>
        ) : null}
        {panel("image-search") ? (
          <TabsContent value="image-search" forceMount className="data-[state=inactive]:hidden">
            <ImageSearch active={tab === "image-search"} />
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}
