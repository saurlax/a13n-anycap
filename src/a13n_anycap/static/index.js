import { React, Button, Input, Page, useAccess, useApp, useQuery, useQueryClient, useTranslation } from "@a13n/console";

const h = React.createElement;
const en = {
  label: "AnyCap API key",
  loading: "Loading AnyCap settings…",
  loadError: "Could not load AnyCap settings.",
  failed: "Could not update the API key.",
  saved: "API key saved.",
  removed: "Workspace API key removed.",
  workspace: "A workspace API key is configured.",
  environment: "This workspace uses a deployment-provided API key.",
  none: "No API key is configured for this workspace.",
  admin: "A workspace administrator can manage its API key.",
  save: "Save API key",
  replace: "Replace API key",
  remove: "Remove workspace key",
  confirm: "Remove the saved AnyCap API key from this workspace?",
};
const zh = {
  label: "AnyCap API Key",
  loading: "正在加载 AnyCap 设置…",
  loadError: "无法加载 AnyCap 设置。",
  failed: "无法更新 API Key。",
  saved: "API Key 已保存。",
  removed: "工作区 API Key 已删除。",
  workspace: "已配置工作区 API Key。",
  environment: "此工作区使用由部署环境提供的 API Key。",
  none: "此工作区尚未配置 API Key。",
  admin: "工作区管理员可以管理 API Key。",
  save: "保存 API Key",
  replace: "更换 API Key",
  remove: "删除工作区密钥",
  confirm: "删除为此工作区保存的 AnyCap API Key？",
};

async function requestCredential(app, init) {
  const response = await app.request("/credential", init);
  if (!response.ok) throw new Error("AnyCap credential request failed");
  return response.json();
}

function CredentialSettings() {
  const app = useApp();
  const { can, workspace } = useAccess();
  const { i18n } = useTranslation();
  const text = i18n.language.startsWith("zh") ? zh : en;
  const cache = useQueryClient();
  const queryKey = app.queryKey("credential");
  const status = useQuery({
    queryKey,
    queryFn: ({ signal }) => requestCredential(app, { signal }),
  });
  const [key, setKey] = React.useState("");
  const [pending, setPending] = React.useState(false);
  const [message, setMessage] = React.useState(null);
  const source = status.data?.source;
  const editable = can("admin") && !workspace?.archived_at;

  async function change(method) {
    if (pending) return;
    setPending(true);
    setMessage(null);
    try {
      const init = method === "PUT"
        ? { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: key }) }
        : { method };
      await cache.cancelQueries({ queryKey });
      const result = await requestCredential(app, init);
      cache.setQueryData(queryKey, result);
      setKey("");
      setMessage({ text: method === "PUT" ? text.saved : text.removed, error: false });
    } catch {
      setMessage({ text: text.failed, error: true });
    } finally {
      setPending(false);
    }
  }

  return h(Page, { title: "AnyCap" },
    h("section", { className: "anycap-settings", "aria-label": text.label },
      h("p", { role: status.isError ? "alert" : "status" },
        status.isPending ? text.loading : status.isError ? text.loadError : text[source]),
      source && editable && h("form", {
        onSubmit: (event) => {
          event.preventDefault();
          if (key) void change("PUT");
        },
      },
      h("label", { htmlFor: "anycap-api-key" }, text.label),
      h(Input, {
        id: "anycap-api-key", type: "password", autoComplete: "new-password", value: key,
        maxLength: 1024, required: true, disabled: pending, onChange: (event) => setKey(event.target.value),
      }),
      h("div", { className: "anycap-settings-actions" },
        h(Button, { type: "submit", disabled: pending || !key }, source === "workspace" ? text.replace : text.save),
        source === "workspace" && h(Button, {
          type: "button", variant: "outline", disabled: pending,
          onClick: () => { if (window.confirm(text.confirm)) void change("DELETE"); },
        }, text.remove),
      )),
      source && !editable && h("p", null, text.admin),
      message && h("p", { role: message.error ? "alert" : "status" }, message.text),
    ),
  );
}

function AnyCapPage() {
  const app = useApp();
  return h(CredentialSettings, { key: app.workspaceId });
}

export default {
  bridge: 1,
  pages: [{ id: "settings", label: { en: "AnyCap", "zh-CN": "AnyCap" }, component: AnyCapPage }],
};
