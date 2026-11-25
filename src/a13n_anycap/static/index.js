import {
  React, Badge, Button, Input, Label, Page, Switch,
  useAccess, useApp, useQuery, useQueryClient, useTranslation,
} from "@a13n/console";

const h = React.createElement;
const featureGroups = [
  ["generation", ["image", "video", "music", "audio"]],
  ["understanding", ["image_read", "video_read", "audio_read"]],
  ["services", ["web", "social", "drive", "page", "snapshot", "collaboration", "account"]],
];
const en = {
  generation: "Generation",
  understanding: "Media understanding",
  services: "Web and services",
  image: "Image generation and editing",
  video: "Video generation and editing",
  music: "Music generation",
  audio: "Speech and audio generation",
  image_read: "Image understanding",
  video_read: "Video understanding",
  audio_read: "Audio understanding",
  web: "Web search, crawl and download",
  social: "Social media",
  drive: "Drive",
  page: "Page publishing",
  snapshot: "Snapshots",
  collaboration: "Annotation and whiteboards",
  account: "Account and feedback",
  apiKey: "API Key",
  credentials: "Credentials",
  access: "Feature controls",
  save: "Save",
  saving: "Saving…",
  saved: "Saved",
  replace: "Replace",
  remove: "Remove",
  configured: "Saved key",
  environment: "Environment key",
  confirmRemove: "Confirm removal",
  cancel: "Cancel",
  loading: "Loading…",
  loadError: "Could not load settings.",
  saveError: "Could not save.",
  removeError: "Could not remove the key.",
  retry: "Retry",
};
const zh = {
  generation: "媒体生成",
  understanding: "媒体理解",
  services: "网页与服务",
  image: "图片生成与编辑",
  video: "视频生成与编辑",
  music: "音乐生成",
  audio: "语音及音频生成",
  image_read: "图像理解",
  video_read: "视频理解",
  audio_read: "音频理解",
  web: "网页搜索、抓取与下载",
  social: "社交媒体",
  drive: "Drive 云盘",
  page: "Page 发布",
  snapshot: "项目快照",
  collaboration: "批注与白板",
  account: "账户与反馈",
  apiKey: "API Key",
  credentials: "凭据",
  access: "功能开关",
  save: "保存",
  saving: "保存中…",
  saved: "已保存",
  replace: "更换",
  remove: "移除",
  configured: "已设置",
  environment: "环境变量",
  confirmRemove: "确认移除",
  cancel: "取消",
  loading: "加载中…",
  loadError: "加载设置失败。",
  saveError: "保存失败。",
  removeError: "移除密钥失败。",
  retry: "重试",
};

async function request(app, path, init) {
  const response = await app.request(path, init);
  if (!response.ok) throw new Error(`AnyCap request failed (${response.status})`);
  return response.json();
}

function put(body) {
  return { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function QueryStatus({ query, text }) {
  if (query.isPending) return h("p", { role: "status", className: "anycap-feedback" }, text.loading);
  if (!query.isError) return null;
  return h("div", { className: "anycap-actions" },
    h("span", { role: "alert", className: "anycap-error" }, text.loadError),
    h(Button, { type: "button", variant: "outline", onClick: () => void query.refetch() }, text.retry),
  );
}

function Feedback({ message }) {
  if (!message) return null;
  return h("span", { role: message.error ? "alert" : "status", className: message.error ? "anycap-error" : "anycap-feedback" },
    message.text,
  );
}

function SettingsSection({ id, title, children }) {
  return h("section", { className: "anycap-section", "aria-labelledby": id },
    h("h2", { id }, title),
    h("div", { className: "anycap-rows" }, children),
  );
}

function CredentialSettings({ app, editable, text }) {
  const cache = useQueryClient();
  const queryKey = app.queryKey("credential");
  const query = useQuery({ queryKey, queryFn: ({ signal }) => request(app, "/credential", { signal }) });
  const [key, setKey] = React.useState("");
  const [pending, setPending] = React.useState(false);
  const [confirmingRemoval, setConfirmingRemoval] = React.useState(false);
  const [message, setMessage] = React.useState(null);
  const source = query.data?.source;

  async function change(method) {
    setPending(true);
    setMessage(null);
    try {
      await cache.cancelQueries({ queryKey });
      const result = await request(app, "/credential", method === "PUT" ? put({ api_key: key }) : { method });
      cache.setQueryData(queryKey, result);
      setKey("");
      setConfirmingRemoval(false);
      if (method === "PUT") setMessage({ text: text.saved });
    } catch {
      setMessage({ text: method === "PUT" ? text.saveError : text.removeError, error: true });
    } finally {
      setPending(false);
    }
  }

  const badge = source && source !== "none" && h(Badge, { variant: "secondary" },
    source === "workspace" ? text.configured : text.environment,
  );
  const fieldLabel = h("div", { className: "anycap-actions" },
    source && editable ? h(Label, { htmlFor: "anycap-key" }, text.apiKey) : h("span", null, text.apiKey),
    badge,
  );

  return h(SettingsSection, { id: "anycap-key-title", title: text.credentials },
    (query.isPending || query.isError) && h("div", { className: "anycap-row" }, h(QueryStatus, { query, text })),
    source && !editable && h("div", { className: "anycap-row" }, fieldLabel),
    source && editable && h("form", {
      onSubmit: (event) => {
        event.preventDefault();
        if (key && !pending && !confirmingRemoval) void change("PUT");
      },
    },
      h("div", { className: "anycap-row anycap-key-row" }, fieldLabel,
        h("div", { className: "anycap-actions anycap-key-controls" },
          h("div", { className: "anycap-input" }, h(Input, {
            id: "anycap-key", type: "password", autoComplete: "new-password", value: key,
            maxLength: 1024, required: true, disabled: pending, onChange: (event) => setKey(event.target.value),
          })),
          h(Button, { type: "submit", disabled: pending || !key || confirmingRemoval, loading: pending },
            source === "workspace" ? text.replace : text.save,
          ),
          source === "workspace" && !confirmingRemoval && h(Button, {
            type: "button", variant: "outline", disabled: pending,
            onClick: () => setConfirmingRemoval(true),
          }, text.remove),
          confirmingRemoval && h(React.Fragment, null,
            h(Button, { type: "button", variant: "destructive-outline", disabled: pending,
              onClick: () => void change("DELETE") }, text.confirmRemove),
            h(Button, { type: "button", variant: "ghost", disabled: pending,
              onClick: () => setConfirmingRemoval(false) }, text.cancel),
          ),
        ),
      ),
      message && h("div", { className: "anycap-row-feedback" }, h(Feedback, { message })),
    ),
  );
}

function FeatureSettings({ feature, enabled, disabled, onChange, text }) {
  const id = `anycap-${feature}`;
  return h("div", { className: "anycap-row anycap-row-main" },
    h(Label, { htmlFor: id }, text[feature]),
    h(Switch, {
      id, "aria-label": text[feature], checked: enabled, disabled, onCheckedChange: onChange,
    }),
  );
}
function PolicySettings({ app, editable, text }) {
  const cache = useQueryClient();
  const queryKey = app.queryKey("policy");
  const query = useQuery({ queryKey, queryFn: ({ signal }) => request(app, "/policy", { signal }) });
  const [draft, setDraft] = React.useState(null);
  const [pending, setPending] = React.useState(false);
  const [message, setMessage] = React.useState(null);
  const policy = draft ?? query.data;

  async function save(event) {
    event.preventDefault();
    if (!draft || pending) return;
    setPending(true);
    setMessage(null);
    try {
      await cache.cancelQueries({ queryKey });
      const result = await request(app, "/policy", put(draft));
      cache.setQueryData(queryKey, result);
      setDraft(null);
      setMessage({ text: text.saved });
    } catch {
      setMessage({ text: text.saveError, error: true });
    } finally {
      setPending(false);
    }
  }

  return h("section", { "aria-label": text.access },
    h(QueryStatus, { query, text }),
    policy && h("form", { className: "anycap-settings", onSubmit: save },
      ...featureGroups.map(([group, features]) =>
        h(SettingsSection, { id: `anycap-${group}`, title: text[group], key: group },
          ...features.map((feature) => h(FeatureSettings, {
            key: feature, feature, enabled: policy[feature].enabled,
            disabled: !editable || pending, text,
            onChange: (enabled) => {
              setDraft({ ...policy, [feature]: { enabled } });
              setMessage(null);
            },
          })),
        )),
      editable && h("div", { className: "anycap-actions anycap-form-actions" },
        h(Feedback, { message }),
        h(Button, { type: "submit", disabled: !draft || pending, loading: pending }, pending ? text.saving : text.save),
      ),
    ),
  );
}

function WorkspaceSettings() {
  const app = useApp();
  const { can, workspace } = useAccess();
  const { i18n } = useTranslation();
  const text = i18n.language.startsWith("zh") ? zh : en;
  const editable = can("admin") && !workspace?.archived_at;
  return h(Page, { title: "AnyCap", className: "anycap-page" },
    h("div", { className: "anycap-settings" },
      h(CredentialSettings, { app, editable, text }),
      h(PolicySettings, { app, editable, text }),
    ),
  );
}

function AnyCapPage() {
  const app = useApp();
  return h(WorkspaceSettings, { key: app.workspaceId });
}

export default {
  bridge: 1,
  pages: [{ id: "settings", label: { en: "AnyCap", "zh-CN": "AnyCap" }, component: AnyCapPage }],
};
