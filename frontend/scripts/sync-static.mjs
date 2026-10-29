// Python wheel 与本地工作台使用同一份已成功构建的网页资源。
import { access, cp, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const source = new URL("../dist/", import.meta.url);
const target = new URL("../../src/quantbot/web/static/", import.meta.url);
await access(new URL("index.html", source));
await rm(target, { recursive: true, force: true });
await cp(fileURLToPath(source), fileURLToPath(target), { recursive: true });
