"""Dive 3D Gen Blender add-on."""

bl_info = {
    "name": "Dive 3D Gen",
    "author": "Dive Smart Factory",
    "version": (1, 0, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Dive 3D",
    "description": "Generate and import textured 3D assets through Dive 3D Gen",
    "category": "3D View",
}

import base64
import json
import mimetypes
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup


DEFAULT_SERVER_URL = "https://rodmeca150102--dive-3d-gen.modal.run"
POLL_SECONDS = 5.0
MAX_WAIT_SECONDS = 20 * 60
GENERATED_COLLECTION = "Dive 3D Gen"


class Dive3DGenError(RuntimeError):
    """An API error that can be displayed directly in Blender."""


def _encode_multipart(fields, image_path):
    boundary = f"----Dive3DGen{uuid.uuid4().hex}"
    chunks = []

    for name, value in fields.items():
        if value is None or value == "":
            continue
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                str(value).encode("utf-8"),
                b"\r\n",
            )
        )

    if image_path:
        image_bytes = Path(image_path).read_bytes()
        filename = Path(image_path).name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    "Content-Disposition: form-data; name=\"image\"; "
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                image_bytes,
                b"\r\n",
            )
        )

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _request_json(url, method, api_key, data=None, content_type=None, timeout=60):
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        raise Dive3DGenError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise Dive3DGenError(f"Connection error: {exc.reason}") from exc


class GenerationTask:
    """Runs HTTP work outside Blender's UI thread."""

    def __init__(
        self, server_url, api_key, model, quality, prompt, image_path, output_path, scene_name
    ):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.quality = quality
        self.prompt = prompt
        self.image_path = image_path
        self.output_path = output_path
        self.scene_name = scene_name
        self.job_id = ""
        self.status = "Submitting job"
        self.error = ""
        self.done = False
        self.cancelled = False
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self._cancel_event.set()

    def snapshot(self):
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "error": self.error,
                "done": self.done,
                "cancelled": self.cancelled,
                "output_path": self.output_path,
            }

    def _set(self, **values):
        with self._lock:
            for name, value in values.items():
                setattr(self, name, value)

    def _run(self):
        try:
            body, content_type = _encode_multipart(
                {
                    "model": self.model,
                    "quality": self.quality,
                    "prompt": self.prompt,
                },
                self.image_path,
            )
            response = _request_json(
                f"{self.server_url}/generate",
                "POST",
                self.api_key,
                data=body,
                content_type=content_type,
            )
            job_id = response.get("job_id")
            if not job_id:
                raise Dive3DGenError("Server response did not include job_id.")

            self._set(job_id=job_id, status="Queued")
            started = time.monotonic()
            while time.monotonic() - started < MAX_WAIT_SECONDS:
                if self._cancel_event.wait(POLL_SECONDS):
                    self._set(status="Cancelled", cancelled=True, done=True)
                    return

                response = _request_json(
                    f"{self.server_url}/status/{job_id}", "GET", self.api_key, timeout=45
                )
                state = response.get("status", "running")
                if state in {"queued", "running"}:
                    elapsed = int(time.monotonic() - started)
                    self._set(status=f"Generating ({elapsed}s)")
                    continue
                if state == "error":
                    raise Dive3DGenError(response.get("detail") or "Generation failed on the server.")
                if state != "done":
                    raise Dive3DGenError(f"Unexpected job status: {state}")

                glb_base64 = response.get("glb_base64")
                if not glb_base64:
                    raise Dive3DGenError("Server completed without GLB data.")
                glb_bytes = base64.b64decode(glb_base64)
                if len(glb_bytes) < 20 or glb_bytes[:4] != b"glTF":
                    raise Dive3DGenError("Server returned invalid GLB data.")

                Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(self.output_path).write_bytes(glb_bytes)
                self._set(status="Importing GLB", done=True)
                return

            raise Dive3DGenError("Timeout: the server took more than 20 minutes.")
        except Exception as exc:
            self._set(error=str(exc) or type(exc).__name__, status="Failed", done=True)


_task = None
_timer_running = False


def _addon_preferences():
    return bpy.context.preferences.addons[__package__].preferences


def _resolve_output_directory(directory):
    directory = directory.strip()
    if directory.startswith("//") and not bpy.data.filepath:
        # Relative Blender paths have no project root until the .blend is saved.
        return Path.home() / "Documents" / "Dive3DGen" / "Generated"
    return Path(bpy.path.abspath(directory or "//Dive3DGen/Generated"))


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _find_layer_collection(layer_collection, collection):
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, collection)
        if found:
            return found
    return None


def _generated_collection(scene):
    collection = bpy.data.collections.get(GENERATED_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(GENERATED_COLLECTION)
    if collection.name not in {child.name for child in scene.collection.children}:
        scene.collection.children.link(collection)
    return collection


def _import_glb(context, output_path, settings, scene=None):
    scene = scene or context.scene
    view_layer = scene.view_layers[0]
    before = set(bpy.data.objects)
    collection = _generated_collection(scene)
    target_layer = _find_layer_collection(view_layer.layer_collection, collection)
    original_layer = view_layer.active_layer_collection
    if target_layer:
        view_layer.active_layer_collection = target_layer

    try:
        with context.temp_override(scene=scene, view_layer=view_layer):
            bpy.ops.object.select_all(action="DESELECT")
            bpy.ops.import_scene.gltf(filepath=output_path)
    finally:
        view_layer.active_layer_collection = original_layer

    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        raise Dive3DGenError("Blender imported no objects from the generated GLB.")

    asset_name = Path(output_path).stem
    roots = [obj for obj in imported if obj.parent not in imported]
    for obj in imported:
        obj["dive_3d_gen_model"] = settings.model
        obj["dive_3d_gen_source"] = output_path
    for index, root in enumerate(roots, start=1):
        root.name = asset_name if len(roots) == 1 else f"{asset_name}_{index:02d}"

    with context.temp_override(scene=scene, view_layer=view_layer):
        bpy.ops.object.select_all(action="DESELECT")
        for obj in roots:
            obj.select_set(True)
        view_layer.objects.active = roots[0]
    settings.last_output_path = output_path
    settings.status = f"Imported {len(imported)} object(s)"


def _task_timer():
    global _task, _timer_running

    if _task is None:
        _timer_running = False
        return None

    snapshot = _task.snapshot()
    scene = bpy.data.scenes.get(_task.scene_name)
    settings = scene.dive_3d_gen if scene else None
    if settings:
        settings.status = snapshot["status"]
        settings.last_job_id = snapshot["job_id"]
        if snapshot["error"]:
            settings.last_error = snapshot["error"]

    if snapshot["done"]:
        if not snapshot["error"] and not snapshot["cancelled"]:
            try:
                if not settings:
                    raise Dive3DGenError("The source Blender scene no longer exists.")
                settings.last_output_path = snapshot["output_path"]
                _import_glb(bpy.context, snapshot["output_path"], settings, scene=scene)
                settings.last_error = ""
            except Exception as exc:
                if settings:
                    settings.last_error = str(exc) or type(exc).__name__
                    settings.status = "GLB saved; import failed"
        _task = None
        _timer_running = False

    _tag_redraw()
    return 0.25 if _timer_running else None


class DIVE3DGEN_Preferences(AddonPreferences):
    bl_idname = __package__

    server_url: StringProperty(name="Server URL", default=DEFAULT_SERVER_URL)
    api_key: StringProperty(name="API Key", subtype="PASSWORD")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "server_url")
        layout.prop(self, "api_key")


class DIVE3DGEN_Settings(PropertyGroup):
    model: EnumProperty(
        name="Model",
        items=(
            ("sf3d", "SF3D", "Fast image-to-3D"),
            ("trellis", "TRELLIS", "Image or text with PBR texture baking"),
            ("hunyuan", "Hunyuan", "Image-to-3D with PBR texture"),
        ),
        default="trellis",
    )
    quality: EnumProperty(
        name="Quality",
        items=(
            ("fast", "Fast", "Fastest generation"),
            ("balanced", "Balanced", "Balanced speed and detail"),
            ("high", "High", "Highest detail and processing time"),
        ),
        default="balanced",
    )
    prompt: StringProperty(name="Prompt")
    image_path: StringProperty(name="Reference Image", subtype="FILE_PATH")
    output_directory: StringProperty(
        name="Output Directory", subtype="DIR_PATH", default="//Dive3DGen/Generated"
    )
    status: StringProperty(default="Ready")
    last_error: StringProperty(default="")
    last_output_path: StringProperty(default="")
    last_job_id: StringProperty(default="")


class DIVE3DGEN_OT_generate(Operator):
    bl_idname = "dive3dgen.generate"
    bl_label = "Generate 3D"
    bl_description = "Send the request to Dive 3D Gen"

    def execute(self, context):
        global _task, _timer_running

        if _task is not None:
            self.report({"WARNING"}, "A generation is already running.")
            return {"CANCELLED"}

        preferences = _addon_preferences()
        settings = context.scene.dive_3d_gen
        server_url = preferences.server_url.strip()
        api_key = preferences.api_key.strip()
        image_path = bpy.path.abspath(settings.image_path) if settings.image_path else ""
        prompt = settings.prompt.strip()

        if not server_url or not api_key:
            self.report({"ERROR"}, "Configure Server URL and API Key first.")
            return {"CANCELLED"}
        if image_path and not Path(image_path).is_file():
            self.report({"ERROR"}, "Reference image was not found.")
            return {"CANCELLED"}
        if settings.model in {"sf3d", "hunyuan"} and not image_path:
            self.report({"ERROR"}, f"{settings.model.upper()} requires a reference image.")
            return {"CANCELLED"}
        if not image_path and not prompt:
            self.report({"ERROR"}, "Provide a prompt or a reference image.")
            return {"CANCELLED"}

        output_dir = _resolve_output_directory(settings.output_directory)
        if settings.output_directory.startswith("//") and not bpy.data.filepath:
            settings.output_directory = str(output_dir)
        filename = f"Dive3D_{time.strftime('%Y%m%d_%H%M%S')}.glb"
        output_path = str(output_dir / filename)
        settings.last_error = ""
        settings.last_output_path = ""
        settings.status = "Submitting job"

        _task = GenerationTask(
            server_url,
            api_key,
            settings.model,
            settings.quality,
            prompt,
            image_path,
            output_path,
            context.scene.name,
        )
        _task.start()
        _timer_running = True
        bpy.app.timers.register(_task_timer, first_interval=0.1)
        return {"FINISHED"}


class DIVE3DGEN_OT_cancel(Operator):
    bl_idname = "dive3dgen.cancel"
    bl_label = "Cancel Generation"

    def execute(self, context):
        if _task is not None:
            _task.cancel()
        return {"FINISHED"}


class DIVE3DGEN_OT_import_last(Operator):
    bl_idname = "dive3dgen.import_last"
    bl_label = "Import Last GLB"

    def execute(self, context):
        settings = context.scene.dive_3d_gen
        output_path = bpy.path.abspath(settings.last_output_path)
        if not output_path or not Path(output_path).is_file():
            self.report({"ERROR"}, "No generated GLB is available to import.")
            return {"CANCELLED"}
        try:
            _import_glb(context, output_path, settings)
            settings.last_error = ""
        except Exception as exc:
            settings.last_error = str(exc) or type(exc).__name__
            self.report({"ERROR"}, settings.last_error)
            return {"CANCELLED"}
        return {"FINISHED"}


class DIVE3DGEN_PT_panel(Panel):
    bl_idname = "DIVE3DGEN_PT_panel"
    bl_label = "Dive 3D Gen"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Dive 3D"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.dive_3d_gen
        preferences = _addon_preferences()
        running = _task is not None

        config = layout.box()
        config.label(text="Connection", icon="URL")
        config.prop(preferences, "server_url")
        config.prop(preferences, "api_key")

        form = layout.box()
        form.label(text="Generation", icon="MESH_UVSPHERE")
        form.prop(settings, "model")
        form.prop(settings, "quality")
        if settings.model != "sf3d":
            form.prop(settings, "prompt")
        form.prop(settings, "image_path")
        form.prop(settings, "output_directory")

        if settings.model in {"sf3d", "hunyuan"}:
            form.label(text="This model requires an image.", icon="INFO")
        elif not settings.image_path:
            form.label(text="TRELLIS can generate from text only.", icon="INFO")

        actions = layout.column(align=True)
        actions.enabled = not running
        actions.operator("dive3dgen.generate", icon="PLAY")
        if running:
            layout.operator("dive3dgen.cancel", icon="CANCEL")

        state = layout.box()
        state.label(text=settings.status, icon="TIME" if running else "CHECKMARK")
        if settings.last_job_id:
            state.label(text=f"Job: {settings.last_job_id}")
        if settings.last_error:
            state.label(text=settings.last_error, icon="ERROR")
        if settings.last_output_path:
            state.label(text=Path(settings.last_output_path).name, icon="FILE_3D")
            state.operator("dive3dgen.import_last", icon="IMPORT")


CLASSES = (
    DIVE3DGEN_Preferences,
    DIVE3DGEN_Settings,
    DIVE3DGEN_OT_generate,
    DIVE3DGEN_OT_cancel,
    DIVE3DGEN_OT_import_last,
    DIVE3DGEN_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dive_3d_gen = PointerProperty(type=DIVE3DGEN_Settings)


def unregister():
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    del bpy.types.Scene.dive_3d_gen
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
