"""Agent-facing operations supported by AnyCap CLI/MCP 0.6.6."""

from dataclasses import dataclass
from typing import Literal

type Feature = Literal[
    "image",
    "video",
    "music",
    "audio",
    "image_read",
    "video_read",
    "audio_read",
    "web",
    "social",
    "drive",
    "page",
    "snapshot",
    "collaboration",
    "account",
]
type FieldKind = Literal[
    "text", "int", "bool", "texts", "file", "files", "tree", "trees", "url", "urls", "params", "media_refs"
]


@dataclass(frozen=True)
class Input:
    name: str
    kind: FieldKind = "text"
    required: bool = False


@dataclass(frozen=True)
class Operation:
    feature: Feature
    command: tuple[str, ...]
    description: str
    flags: tuple[Input, ...] = ()
    arguments: tuple[Input, ...] = ()
    effect: Literal["read", "write"] = "write"
    transport: Literal["cli", "mcp", "environment"] = "cli"
    output: Literal["file", "directory", "optional"] | None = None
    model: bool = False
    project_config: bool = False

    @property
    def inputs(self) -> tuple[Input, ...]:
        inputs = self.arguments + self.flags
        if self.output:
            inputs += (Input("output_path", required=self.output != "optional"),)
        if self.project_config:
            inputs += (Input("project_dir"),)
        return inputs


model = Input("model")
prompt = Input("prompt", required=True)
mode = Input("mode")
params = Input("param", "params")
generate = (model, prompt, mode, params)
analysis = (Input("instruction"), model)
node = (Input("node_id"),)
parent = (Input("parent"), Input("parent_path"))
source = (Input("src_path"),)
page = (Input("page_id"), Input("target"))
version = Input("version", "int")
session = Input("session", required=True)

OPERATIONS: dict[str, Operation] = {
    "image_generate": Operation(
        "image",
        ("image", "generate"),
        "Generate images using any supported mode.",
        generate,
        output="optional",
        model=True,
    ),
    "image_edit": Operation(
        "image",
        ("anycap_image_edit",),
        "Edit referenced images using AnyCap MCP.",
        (
            prompt,
            Input("images", "media_refs", True),
            model,
            mode,
            Input("aspect_ratio"),
            Input("format"),
            Input("negative_prompt"),
            Input("resolution"),
            Input("move_forward", "int"),
            Input("rotate_degrees", "int"),
            Input("vertical_tilt", "int"),
            Input("use_wide_angle", "bool"),
        ),
        transport="mcp",
        model=True,
    ),
    "image_upscale": Operation(
        "image",
        ("anycap_image_upscale",),
        "Upscale images using AnyCap MCP.",
        (
            Input("images", "media_refs", True),
            model,
            Input("prompt"),
            Input("aspect_ratio"),
            Input("format"),
            Input("resolution"),
        ),
        transport="mcp",
        model=True,
    ),
    "video_generate": Operation(
        "video",
        ("video", "generate"),
        "Generate, animate or edit a video asynchronously.",
        generate + (Input("no_wait", "bool"), Input("timeout")),
        output="optional",
        model=True,
    ),
    "video_task_get": Operation(
        "video",
        ("video", "tasks", "get"),
        "Read an AnyCap video task and its results.",
        arguments=(Input("task_id", required=True),),
        effect="read",
    ),
    "video_task_wait": Operation(
        "video",
        ("video", "tasks", "wait"),
        "Wait for and optionally save a video result.",
        (Input("timeout"),),
        (Input("task_id", required=True),),
        effect="read",
        output="optional",
    ),
    "music_generate": Operation(
        "music",
        ("music", "generate"),
        "Generate a song or instrumental.",
        generate + (Input("instrumental", "bool"), Input("lyrics"), Input("tags"), Input("title")),
        output="optional",
        model=True,
    ),
    "audio_generate": Operation(
        "audio",
        ("audio", "generate"),
        "Generate speech or audio from text or media.",
        generate,
        output="optional",
        model=True,
    ),
    "image_read": Operation(
        "image_read",
        ("actions", "image-read"),
        "Analyze, compare or extract text from images.",
        (Input("url", "urls"), Input("file", "files")) + analysis,
        model=True,
    ),
    "video_read": Operation(
        "video_read",
        ("actions", "video-read"),
        "Analyze a video or extract information.",
        (Input("url", "url"), Input("file", "file")) + analysis,
        model=True,
    ),
    "audio_read": Operation(
        "audio_read",
        ("actions", "audio-read"),
        "Transcribe or analyze audio.",
        (Input("url", "url"), Input("file", "file")) + analysis,
        model=True,
    ),
    "web_search": Operation(
        "web",
        ("search",),
        "Run a structured query or grounded search prompt.",
        (
            Input("query"),
            Input("prompt"),
            Input("max_results", "int"),
            Input("no_crawl", "bool"),
            Input("include", "texts"),
            Input("exclude", "texts"),
            Input("after"),
            Input("before"),
            Input("time_range"),
        ),
    ),
    "web_crawl": Operation(
        "web", ("crawl",), "Read a public web page as Markdown.", arguments=(Input("url", "url", True),)
    ),
    "web_download": Operation(
        "web",
        ("download",),
        "Download a URL into the Agent Environment.",
        arguments=(Input("url", "url", True),),
        transport="environment",
        output="file",
    ),
    "social_get": Operation(
        "social", ("social", "get"), "Look up an Instagram or X post.", (Input("url", "url", True),)
    ),
    "social_profile": Operation(
        "social",
        ("social", "profile"),
        "Look up an Instagram or X profile.",
        (Input("platform", required=True), Input("username", required=True)),
    ),
    "social_list": Operation(
        "social",
        ("social", "list"),
        "List one batch of social posts or reels.",
        (Input("platform", required=True), Input("user_id"), Input("username"), Input("type")),
    ),
    "social_search": Operation(
        "social",
        ("social", "search"),
        "Search one batch of public X posts.",
        (Input("platform", required=True), Input("query", required=True)),
    ),
    "drive_list": Operation("drive", ("drive", "ls"), "List Drive nodes in a folder.", parent, effect="read"),
    "drive_get": Operation(
        "drive",
        ("anycap_drive_get",),
        "Get a Drive node by ID.",
        (Input("node_id", required=True),),
        effect="read",
        transport="mcp",
    ),
    "drive_resolve_path": Operation(
        "drive",
        ("anycap_drive_resolve_path",),
        "Find the Drive node at an absolute path.",
        (Input("path", required=True),),
        effect="read",
        transport="mcp",
    ),
    "drive_mkdir": Operation(
        "drive", ("drive", "mkdir"), "Create a Drive folder.", parent + (Input("name", required=True),)
    ),
    "drive_move": Operation(
        "drive",
        ("drive", "mv"),
        "Move or rename a Drive node.",
        source + (Input("dst_parent"), Input("dst_parent_path"), Input("name")),
        node,
    ),
    "drive_delete": Operation(
        "drive", ("drive", "rm"), "Delete a Drive node, including children for folders.", source, node
    ),
    "drive_upload": Operation(
        "drive",
        ("drive", "upload"),
        "Upload a file from the Agent Environment.",
        parent,
        (Input("file", "file", True),),
    ),
    "drive_share": Operation(
        "drive",
        ("drive", "share"),
        "Create or update a Drive share.",
        source + (Input("expires"), Input("no_expire", "bool"), Input("password"), Input("no_password", "bool")),
        node,
    ),
    "drive_rotate_share": Operation("drive", ("drive", "share", "rotate"), "Rotate a Drive share token.", source, node),
    "drive_unshare": Operation("drive", ("drive", "unshare"), "Revoke a Drive share.", source, node),
    "page_list": Operation(
        "page",
        ("page", "list"),
        "List server sites or project-local targets.",
        (Input("local", "bool"),),
        effect="read",
        project_config=True,
    ),
    "page_info": Operation(
        "page", ("page", "info"), "Get a Page site's settings and status.", page, effect="read", project_config=True
    ),
    "page_versions": Operation(
        "page", ("page", "versions"), "List versions for a Page site.", page, effect="read", project_config=True
    ),
    "page_create": Operation(
        "page",
        ("page", "create"),
        "Create a Page site without deploying.",
        (Input("name", required=True), Input("target")),
        project_config=True,
    ),
    "page_deploy": Operation(
        "page",
        ("page", "deploy"),
        "Deploy an HTML file or static site from the Agent Environment.",
        page + (Input("name"), Input("new", "bool"), Input("publish", "bool"), Input("concurrency", "int")),
        (Input("path", "tree", True),),
        project_config=True,
    ),
    "page_set": Operation(
        "page",
        ("page", "set"),
        "Change Page settings, visibility, password or SPA fallback.",
        page
        + (
            Input("name"),
            Input("access"),
            Input("password"),
            Input("no_password", "bool"),
            Input("badge", "bool"),
            Input("no_badge", "bool"),
            Input("spa_fallback", "bool"),
            Input("no_spa_fallback", "bool"),
        ),
        project_config=True,
    ),
    "page_publish": Operation(
        "page",
        ("page", "publish"),
        "Publish the selected or latest verified version.",
        page + (version,),
        project_config=True,
    ),
    "page_unpublish": Operation("page", ("page", "unpublish"), "Unpublish a Page site.", page, project_config=True),
    "page_rollback": Operation(
        "page",
        ("page", "rollback"),
        "Clone a historical Page version.",
        page + (Input("version", "int", True),),
        project_config=True,
    ),
    "page_delete": Operation("page", ("page", "delete"), "Delete a Page site.", page, project_config=True),
    "snapshot_create": Operation(
        "snapshot",
        ("snapshot", "create"),
        "Archive workspace paths and create a share URL.",
        (
            Input("target", "trees", True),
            Input("name"),
            Input("expires"),
            Input("handoff_file", "file"),
            Input("overwrite", "bool"),
        ),
    ),
    "snapshot_restore": Operation(
        "snapshot",
        ("snapshot", "restore"),
        "Restore a Drive snapshot into a new workspace directory.",
        (Input("url", "url", True),),
        output="directory",
    ),
    "annotate_start": Operation(
        "collaboration",
        ("annotate",),
        "Start a media annotation session in the Agent Environment.",
        (Input("port", "int"), Input("output")),
        (Input("target", required=True),),
        transport="environment",
    ),
    "annotate_list": Operation(
        "collaboration",
        ("annotate", "list"),
        "List active annotation sessions.",
        effect="read",
        transport="environment",
    ),
    "annotate_poll": Operation(
        "collaboration",
        ("annotate", "poll"),
        "Get annotation feedback and recording paths.",
        (session,),
        effect="read",
        transport="environment",
    ),
    "annotate_stop": Operation(
        "collaboration", ("annotate", "stop"), "Stop an annotation session.", (session,), transport="environment"
    ),
    "draw_start": Operation(
        "collaboration",
        ("draw",),
        "Open a whiteboard in the Agent Environment.",
        (Input("init", "file"), Input("image", "file"), Input("port", "int"), Input("output")),
        transport="environment",
    ),
    "draw_list": Operation(
        "collaboration", ("draw", "list"), "List active whiteboards.", effect="read", transport="environment"
    ),
    "draw_poll": Operation(
        "collaboration",
        ("draw", "poll"),
        "Read the current whiteboard result.",
        (session,),
        effect="read",
        transport="environment",
    ),
    "draw_update": Operation(
        "collaboration",
        ("draw", "update"),
        "Replace an active whiteboard's diagram.",
        (session, Input("init", "file", True)),
        transport="environment",
    ),
    "draw_stop": Operation(
        "collaboration", ("draw", "stop"), "Stop a whiteboard session.", (session,), transport="environment"
    ),
    "account_status": Operation(
        "account", ("anycap_status",), "Read the AnyCap account and server status.", effect="read", transport="mcp"
    ),
    "feedback_submit": Operation(
        "account",
        ("feedback",),
        "Send feedback to AnyCap.",
        (
            Input("message", required=True),
            Input("type", required=True),
            Input("context"),
            Input("diagnostics"),
            Input("request_id"),
            Input("trace_id"),
        ),
    ),
}
