# a13n-anycap

An App plugin that gives a13n Service agents direct access to AnyCap's HTTPS API. Generate a video, check its progress in a later Run, or synthesize a short speech clip.

| Tool | What it does |
| --- | --- |
| `anycap_list_models` | List live video or audio models. |
| `anycap_model_schema` | Get the selected model's text-to-video or text-to-audio schema. |
| `anycap_submit_video` | Queue an AnyCap text-to-video task and return its task handle. |
| `anycap_get_video_task` | Read that task's status and refresh the download links when it succeeds. |
| `anycap_generate_audio` | Generate a short text-to-audio clip and return its download links. |

## Install

Use Python 3.13 and a13n Service 0.3.2, the versions tested by this repository. Build the wheel with `uv build --wheel` and install the same wheel into every Service control and worker Python environment. See the [Service App plugin installation guide](https://github.com/converge-ai-labs/agent-foundation/blob/main/docs/a13n-service/app-plugins.md#deploy-app-plugins) for local and container deployment.

1. Provide `A13N_ANYCAP_WORKSPACE_KEYS` to each Service process through the deployment's secret configuration. Its value is a JSON object mapping a workspace ID to an AnyCap API key, for example `{"ws_example":"<AnyCap API key>"}`. Each binding uses that key's AnyCap account and credits. Restart Service after updating the bindings.
2. In Console, open **Settings → Organization → App plugins** and enable **AnyCap**.
3. Add the AnyCap plugin to the Agent's configuration. The equivalent Agent config fragment is below.

```json
{
  "plugins": [
    {
      "instance_name": "anycap",
      "plugin_key": "a13n-anycap",
      "config": {}
    }
  ]
}
```

Runs with an outbound host allowlist must include `api.anycap.ai`. Generation requires approval by default because it spends AnyCap credits. Set `require_generation_approval` to `false` in the plugin instance's config for unattended generation.

## Generate and retrieve a video

Ask an Agent with AnyCap enabled to look up a text-to-video model and submit a short clip using that model's supported duration and aspect ratio. Approve the tool call if prompted. Save the returned `task_handle`, then ask the Agent to check that handle in a later Run. If the video is still processing, the tool reports its status and AnyCap's suggested polling interval. A successful lookup returns current download links. For speech, ask the Agent to inspect a text-to-audio model and synthesize a short line.

Task handles are scoped to the workspace and its configured API key. Rotate the key after retrieving any still-needed videos. A signed download link grants access to its holder until expiry. Download results while the links are valid, or fetch a fresh video link with the task handle. The plugin returns links without copying media into Service storage.

A generation request can reach AnyCap even if the response is lost. The tool reports the trace ID when the outcome is unknown. Check that request in AnyCap before starting another generation. Only video has a separate task API. Audio waits for an HTTP response for up to 55 seconds and is intended for short clips.

This release covers text inputs and links. Reference uploads, video editing, music generation, and a Console account management page are outside its scope.

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
