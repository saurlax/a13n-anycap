# a13n-anycap

An a13n Service App plugin for AnyCap media, web, social, delivery, and collaboration tools.

| Feature | Operations |
| --- | --- |
| Media generation | Generate and edit images and video, upscale images, generate music, and synthesize speech or audio. Discover model schemas and look up or wait for video tasks. |
| Media understanding | Analyze images and video, transcribe and analyze audio, and compare images. |
| Web and social | Run structured or grounded search, crawl and download pages, and look up social profiles, posts, and feeds. |
| Drive and Page | List, resolve, upload, move, delete, and share files. Create, deploy, publish, unpublish, roll back, and configure sites. |
| Snapshots and collaboration | Create and restore snapshots. Start, list, update, poll, and stop applicable annotation and whiteboard sessions. |
| Account | Check account status and send feedback. |

| Tool | What it does |
| --- | --- |
| `anycap_list_models` | List live video or audio models. |
| `anycap_model_schema` | Get the selected model's text-to-video or text-to-audio schema. |
| `anycap_submit_video` | Queue an AnyCap text-to-video task and return its task handle. |
| `anycap_get_video_task` | Read that task's status and refresh the download links when it succeeds. |
| `anycap_generate_audio` | Generate a short text-to-audio clip and return its download links. |
| `anycap_operations` | Discover operations enabled for the workspace and their input fields. |
| `anycap_read` | Execute an operation marked read-only in discovery. |
| `anycap_execute` | Execute a billable operation or an operation that changes remote or workspace state. |

## Install

Use Python 3.13 and a13n Service 0.3.2, the versions tested by this repository. Build the wheel with `uv build --wheel` and install the same wheel into every Service control and worker Python environment. See the [Service App plugin installation guide](https://github.com/converge-ai-labs/agent-foundation/blob/main/docs/a13n-service/app-plugins.md#deploy-app-plugins) for local and container deployment.

Install the native [AnyCap CLI 0.6.6](https://docs.anycap.ai/getting-started/install/) on every Service worker. Set `A13N_ANYCAP_CLI_PATH` to its absolute path when it is outside `PATH`. Install the CLI in the Agent Environment as well for downloads, annotation, and whiteboards. Open collaboration URLs in a browser that can reach that Environment's loopback address.

1. In Console, open **Settings → Organization → App plugins** and enable **AnyCap**.
2. Open **AnyCap** in the target workspace and save an API key as a workspace administrator. Service encrypts the key using its configured encryption key ring. Saving, replacing, or removing a workspace key takes effect without a Service restart.
3. Add the AnyCap plugin to the Agent's configuration. The equivalent Agent config fragment is below.

```json
{
  "plugins": [
    {
      "instance_name": "anycap",
      "plugin_key": "saurlax.anycap",
      "config": {}
    }
  ]
}
```

Runs with an outbound host allowlist must include `api.anycap.ai` and any input URLs they use. Paid operations and operations with side effects require approval by default. Set `require_generation_approval` to `false` in the plugin instance's config for unattended execution.

On the **AnyCap** workspace page, administrators can enable or disable each feature. Features default to enabled. Disabling video also turns off video task lookups. Agents can still discover available models and schemas for enabled generation features.

For headless configuration, set `A13N_ANYCAP_WORKSPACE_KEYS` in each Service process to a JSON mapping such as `{"ws_example":"<AnyCap API key>"}` and restart Service. A saved workspace key takes precedence over the environment mapping. Removing a saved workspace key restores its environment binding when one exists.

## Generate and retrieve a video

Ask an Agent with AnyCap enabled to look up a text-to-video model and submit a short clip using that model's supported duration and aspect ratio. Approve the tool call if prompted. Save the returned `task_handle`, then ask the Agent to check that handle in a later Run. If the video is still processing, the tool reports its status and AnyCap's suggested polling interval. A successful lookup returns current download links. For speech, ask the Agent to inspect a text-to-audio model and synthesize a short line.

Task handles are scoped to the workspace and its configured API key. Rotate the key after retrieving any still-needed videos. A signed download link grants access to its holder until expiry. Download results while the links are valid, or fetch a fresh video link with the task handle. The plugin returns links without copying media into Service storage.

A generation request can reach AnyCap even if the response is lost. The tool reports the trace ID when the outcome is unknown. Check that request in AnyCap before starting another generation. Only video has a separate task API. Audio waits for an HTTP response for up to 55 seconds and is intended for short clips.

Discover operation inputs using `anycap_operations`. File and directory arguments refer to the Agent Environment. In a generation parameter or MCP image reference, pass a workspace file as `{"workspace_path":"/workspace/input.png"}`. Use `output_path` to save generated content to the Environment or restore a snapshot to a new directory. Use a workspace `project_dir` for named Page targets stored in `anycap.toml`.

AnyCap authenticates with the workspace API key. CLI login, account switching, updater, skill installation, and the standalone agent daemon are managed by the operator.

## Develop

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv build --wheel
```

Tests exercise discovery by Service, approval, typed API requests, cross-Run task lookup, workspace isolation, and timeout behavior using HTTP fixtures.

## License

[Apache-2.0](LICENSE)
