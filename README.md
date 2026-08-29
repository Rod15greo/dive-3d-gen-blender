# Dive 3D Gen for Blender

Generate textured GLB assets from reference images or text prompts directly in
Blender. The add-on uses the Dive 3D Gen HTTP API and imports completed GLBs
into a dedicated Blender collection with their PBR materials intact.

## Requirements

- Blender 4.2 or newer.
- A Dive 3D Gen server URL and API key.

## Install

1. Download [Dive3DGen_Blender_Addon.zip](./Dive3DGen_Blender_Addon.zip).
2. In Blender, open `Edit > Preferences > Add-ons`.
3. Choose **Install from Disk**, select the ZIP, and enable **Dive 3D Gen**.
4. In a 3D Viewport, press `N` and open the **Dive 3D** tab.

## Use

1. Keep the hosted server URL or enter your own compatible endpoint.
2. Enter an API key. Request one from the hosted server operator, or create one
   after deploying your own server.
3. Select SF3D, TRELLIS, or Hunyuan and a quality preset.
4. Add a reference image. TRELLIS can also generate from a text prompt.
5. Click **Generate 3D**.

Completed files are saved to `//Dive3DGen/Generated` relative to the saved
`.blend` file. For an unsaved Blender file, the add-on safely uses
`Documents/Dive3DGen/Generated`. The GLB is imported into the `Dive 3D Gen`
collection.

SF3D and Hunyuan require an image. TRELLIS accepts an image, a prompt, or both.

## Server options

The default endpoint is operated by Dive Smart Factory. API keys are required
because every generation uses the operator's GPU budget. To self-host, deploy
[dive-3d-gen-server](https://github.com/Rod15greo/dive-3d-gen-server) and put
its endpoint in the add-on panel.

## Development

The distributable ZIP contains only `dive_3d_gen/__init__.py`. After modifying
the source, recreate `Dive3DGen_Blender_Addon.zip` before distributing it.

## License

Add-on code is MIT-licensed. Generated model output and upstream model use are
subject to the terms of the selected model and the server operator.
