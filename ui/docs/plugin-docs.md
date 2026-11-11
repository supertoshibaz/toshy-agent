# Creating plugins for Toshy

A detailed guide for developers opening Toshy's source code for the first time.

**Revision:** October 21, 2026. **Package format:** `schema_version: 1`. **Plugin API:** `1`.

This guide was checked against the `toshy-to-gpt-11` project. The examples describe the existing implementation, not planned features. If your version's interface differs, first check your Toshy version and the components included in the installed plugin update.

The examples passed Python and JSON syntax checks, manifest validation, and packaging checks using the project's own tools. The counter example was also tested with real Qt widgets in offscreen mode; adding a second component, data persistence, a slot, an event handler, a timer, a simple replacement, and a tool-call wrapper were checked. This does not replace testing your finished plugin in a complete working configuration and on the operating systems you claim to support.

## Contents

1. [What is a plugin](#1-what-is-a-plugin)
2. [Terms: name, ID, component, and template](#2-terms-name-id-component-and-template)
3. [What a developer needs](#3-what-a-developer-needs)
4. [How the plugin manager works](#4-how-the-plugin-manager-works)
5. [Your first plugin: a counter widget](#5-your-first-plugin-a-counter-widget)
6. [How to read and edit plugin.json](#6-how-to-read-and-edit-pluginjson)
7. [Multiple components in one plugin](#7-multiple-components-in-one-plugin)
8. [What each template creates](#8-what-each-template-creates)
9. [A module with a tool for the agent](#9-a-module-with-a-tool-for-the-agent)
10. [Pipeline nodes](#10-pipeline-nodes)
11. [Context API and lifecycle](#11-context-api-and-lifecycle)
12. [Extending existing functionality](#12-extending-existing-functionality)
13. [Replacing built-in components](#13-replacing-built-in-components)
14. [Dependencies, priorities, and conflicts](#14-dependencies-priorities-and-conflicts)
15. [Files, data, and portability](#15-files-data-and-portability)
16. [Export, installation, and releasing an update](#16-export-installation-and-releasing-an-update)
17. [Publishing on GitHub and in the registry](#17-publishing-on-github-and-in-the-registry)
18. [How to test and debug a plugin](#18-how-to-test-and-debug-a-plugin)
19. [Common mistakes](#19-common-mistakes)
20. [Where to look in Toshy's source code](#20-where-to-look-in-toshys-source-code)
21. [Pre-publication checklist](#21-pre-publication-checklist)

## 1. What is a plugin

A plugin is a separately installable package that adds or changes Toshy's capabilities. For example, it can add an agent tool, a sidebar widget, a new workspace tab, or an extension to an existing panel.

One plugin can contain multiple modules, multiple widgets, a workspace, top-bar buttons, and event handlers at the same time. All of these are distributed in a single ZIP archive and enabled with a single plugin switch.

A plugin keeps its code inside its own package. Installing it should not replace `run_ui.py`, rewrite the chat's source files, or copy modified files over `ui/` and `core/`. Toshy reads the package description and connects its parts through the plugin system.

A plugin is real Python code with the same access as the Toshy process. It is not an isolated sandbox or a declarative visual customization. Validating the ZIP structure does not prove that the code is safe to execute.

### How a package becomes working functionality

1. The manager validates the archive and reads `plugin.json`.
2. The package is installed into a separate folder for its ID and version.
3. The user enables the plugin.
4. After a full application restart, Toshy checks compatibility, dependencies, and conflicts.
5. Python functions or modules are loaded for the selected components.
6. Interface components are created in the UI process, while agent components are created in the core process.

**Installing, enabling, and starting a module are different actions.** For an ordinary widget, enabling the plugin and restarting the application is sufficient. If a plugin adds an agent module, that module must also be enabled and available to the relevant project under Toshy's normal rules.

## 2. Terms: name, ID, component, and template

| Term | Meaning | Example |
| --- | --- | --- |
| Plugin name | Human-readable name shown in the list | `Text Helper` |
| Plugin ID | Permanent technical identity of the package | `alex.text-helper` |
| Plugin version | Version of the package's code and description | `1.0.0` |
| Component | One attachable part of a plugin | Widget, module, button |
| Component ID | Unique name for a part within the plugin | `text-widget` |
| Full component ID | Plugin ID and local ID separated by `/` | `alex.text-helper/text-widget` |
| System module name | Name of the module in the agent registry | `alex_text_tools` |
| Template | Initial scaffold of files and metadata | `Widget` |
| Tag | Label used for search and classification | `widget`, `text-tools` |
| Entry point | Python function called by Toshy | `plugin:create` |
| Target, `target` | Existing component or extension point | `ui.topbar.right` |

### Why both a name and an ID are needed

The name can be written in Russian and changed later. It is intended for the user.

The manager uses the ID to install updates, locate plugin data, and describe dependencies. If you change the ID of a published plugin, Toshy will treat the package as a different plugin. Previous data and dependencies will not migrate automatically.

For a new plugin, use your own prefix: `alex.counter`, `myteam.project-tools`. IDs may contain lowercase Latin letters, digits, periods, and hyphens as permitted by the format. Start with a letter; do not use spaces, Cyrillic characters, or underscores. A reliable pattern is `author.my-plugin`.

The component ID is assigned separately: `main`, `counter`, `text_tools`, `status-button`. It must be unique within the plugin. Prefer simple lowercase names beginning with a letter; separate words using a single `_` or `-`.

The system module name is yet another name. It must be a Python identifier, such as `alex_text_tools`, without periods or hyphens. Module names must be unique across all of Toshy, including system and custom modules. Give tool names their own prefixes as well.

### Components — an example

The `alex.text-helper` plugin might contain:

- `tools` — a system module for text processing;
- `text-widget` — a widget with buttons for the user;
- `workspace` — a separate workspace tab;
- `toolbar` — a top-bar button.

These are four components of one package. They do not have to live in the same Python file. Conversely, not every Python file needs to be a component: `utils.py` with helper functions is just an internal file.

### A template is neither a tag nor a restriction

Selecting a template **creates files and the initial component description**. A tag only helps users find the package in the Library.

The "Widget" template does not prevent you from adding a module or workspace to the same plugin. "No template" creates an empty package scaffold; you must add behavior yourself or use the "Add component…" button.

The scaffold does not generate working business logic from a description. For example, describing a plugin as a "calculator" will not turn the default widget into a calculator; you need to implement that behavior in Python.

## 3. What a developer needs

- A working copy of Toshy with plugin manager support.
- The same Python interpreter and environment used to launch Toshy.
- A code editor and basic Python knowledge.
- For UI work, Qt fundamentals: `QWidget`, layouts, signals, and slots.
- For tools, an understanding of `async def` and JSON Schema.

You do not need to study the agent's entire source code first. The example below is sufficient for your first independent widget. Modifying the built-in chat requires additional study of its interface and available extension points.

Create a separate source folder, such as `ToshyPluginSources`. Keep it separate from installed versions in `plugins/packages/`. For experiments, it is convenient to have a separate Toshy copy without important working data.

Git is useful for version history but is not required to create and import a ZIP locally. GitHub authentication is not needed to install a local package or download a ready-made package from a public release.

## 4. How the plugin manager works

Open Toshy's top-bar context menu and select "Plugins". The manager has three tabs: "Installed", "Library", and "Export".

### 4.1. Installed

Installed packages appear here. Each card has a name, description, image, tags, version, and enable switch.

Change the order by dragging a card's handle. Higher means higher priority. Hover over the yellow warning icon to see the reasons for conflicts or incompatibility.

The switch saves your choice for the next launch. The message "Changes will apply after restarting the application" means fully quitting and relaunching Toshy. Stopping and restarting only the core is not enough.

The installed package's context menu lets you select it for export or remove it. Exporting an installed package gives you its current contents; it does not automatically create an editable development project. Keep your own source copy separately.

### 4.2. Library

This section contains:

| Element | Purpose |
| --- | --- |
| Plugin catalog repository | Address of the shared catalog; it is not a link to your current plugin's source code |
| Save | Save the catalog address |
| Refresh list | Load packages from the selected catalog |
| Import ZIP… | Install a ready-made local package |
| Import from GitHub… | Download a ready-made package from a public GitHub Release |
| Search and tags | Filter the catalog list |
| Install / Update | Install the selected version when the user explicitly requests it |

In this revision, the default official address may be empty. This does not prevent local imports or direct imports from GitHub.

### 4.3. Export: what each field means

| Element | What to enter or select |
| --- | --- |
| Plugin source | The folder containing `plugin.json` directly inside it |
| Folder… | Select an existing source folder |
| Template | Type of scaffold to create, or the next component to add |
| ID | Permanent ID of the **entire new plugin**, such as `alex.counter` |
| Name | Human-readable name of the new plugin |
| Author | Name, alias, or team name |
| Description | Brief explanation of the package's functionality |
| Image / PNG… | PNG for the new scaffold; a default image is created if left blank |
| Components | List of parts in the selected package and their count |
| Add component… | Create another part in the selected source folder |
| Create source… | Create **a new plugin folder**, not install or run the plugin |
| Inspect package | Validate metadata and files; show what will go into the ZIP |
| Export ZIP… | Package the validated sources into an installable archive |

**An important detail of the current interface:** the ID, name, author, description, and image fields are used by the "Create source…" button. They are not a metadata editor for an already selected plugin. For an existing package, edit `plugin.json` and the corresponding files in its source folder.

Selecting an item in the "Components" list does not open or modify a Python code editor either. This list is an overview of the package. There is currently no built-in component removal or `extend` / `replace` mode editor; make those changes manually in the files.

The "Create source…" button first asks for a parent folder. A subfolder named after the plugin ID is created inside it. For example, selecting `ToshyPluginSources` with ID `alex.counter` produces `ToshyPluginSources/alex.counter/`.

The "Plugin source" field does not determine where this new folder is created; a separate dialog is used for creation. After successful creation, the field automatically switches to the resulting folder.

## 5. Your first plugin: a counter widget

This example adds an independent sidebar widget. It does not require the core, a model, or any third-party Python dependencies beyond the Toshy environment.

### Step 1. Create a scaffold

Under "Plugins → Export", enter:

| Field | Value |
| --- | --- |
| Template | Widget |
| ID | `author.counter` — replace `author` with your own prefix before publishing |
| Name | Counter |
| Author | Your name |
| Description | A simple widget with a click counter |
| Image | Can be left blank |

Click "Create source…" and select a parent folder. Do not select an existing project with the same ID as the destination: the generator does not overwrite existing folders.

The new folder will contain `plugin.json`, `plugin.py`, `README.md`, `LICENSE`, and `icon.png`. Keep all of these files.

### Step 2. Write the code

Open the generated `plugin.py` in an editor and replace its contents:

```python
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton


class CounterWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.count = 0

        layout = QVBoxLayout(self)
        self.label = QLabel("Clicks: 0", self)
        button = QPushButton("Add 1", self)
        button.clicked.connect(self.increment)

        layout.addWidget(self.label)
        layout.addWidget(button)
        layout.addStretch()

    def increment(self):
        self.count += 1
        self.label.setText(f"Clicks: {self.count}")


def create(context):
    return CounterWidget(context.parent)
```

`create` is the entry point. Toshy calls it with a context, obtains a `QWidget`, and places it in its own sidebar panel with a title and controls for moving it. To **add** a widget, you do not need to create another `DockPanel` yourself.

Do not create a `QApplication` or call `app.exec()` in plugin code: the application is already running. This file is also not intended to be run as a standalone program with `python plugin.py`.

The counter in this example is stored only in memory, so it resets to zero after a restart. Data persistence is covered separately below.

### Step 3. Check the manifest

Check the generated `plugin.json`. The following is a complete valid version for this example; the generator has already created `LICENSE` and `icon.png`.

```json
{
  "schema_version": 1,
  "id": "author.counter",
  "version": "1.0.0",
  "name": "Counter",
  "description": "A simple widget with a click counter",
  "author": "Your name",
  "license": "MIT",
  "image": "icon.png",
  "tags": ["productivity"],
  "compatibility": {
    "api": 1,
    "python": ">=3.12",
    "platforms": ["linux", "windows"]
  },
  "files": ["LICENSE", "README.md", "icon.png", "plugin.py"],
  "components": [
    {
      "id": "main",
      "type": "widget",
      "mode": "add",
      "title": "Counter",
      "entrypoint": "plugin:create",
      "placement": "right_top"
    }
  ]
}
```

### Step 4. Export

1. Make sure "Plugin source" points to the folder containing this `plugin.json`.
2. Click "Inspect package".
3. Review the list of included files.
4. Click "Export ZIP…".
5. Save the archive **outside the source folder**.

Exporting does not install the plugin or run its code. It prepares the package for installation.

### Step 5. Install and enable

1. Open the "Library" tab.
2. Click "Import ZIP…" and select the resulting archive.
3. Read the confirmation about installing third-party code.
4. Go to "Installed" and enable "Counter".
5. Fully quit Toshy and start it again.

The widget should appear in the upper-right area. Clicking its button increases the count. If it does not appear, check the switch, warnings, and log; stopping the core instead of restarting the application is not sufficient.

### Step 6. Update the installed example correctly

Edit the original `plugin.py`, not the copy in `plugins/packages/`. After changing a previously installed package, increment `version`, for example to `1.0.1`, validate the contents again, export, import, and restart Toshy.

Installing a different build with the same ID and version is prohibited: a version should identify a specific set of package bytes. Removing and reinstalling is not the normal way to release updates; increase the version instead.

## 6. How to read and edit plugin.json

`plugin.json` is a manifest describing **what to load**, where the code is located, and what the package is compatible with. It must be at the root of both the source folder and the exported ZIP.

It is ordinary UTF-8 JSON: double quotes, no comments, and no trailing commas. Do not add arbitrary fields: the validator rejects unknown keys.

### 6.1. Package-level fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Manifest schema version; currently `1` |
| `id` | Stable plugin ID |
| `version` | Final numeric version, such as `1.0.0`, `1.0.1`, `2.0.0` |
| `name` | User-facing name |
| `description` | Description of functionality |
| `author` | Author or team |
| `license` | License identifier; a `LICENSE` file is also required |
| `image` | Relative path to PNG, JPEG, or WebP |
| `tags` | Additional English tags; an empty list is allowed |
| `compatibility` | Requirements for API, Python, and platform |
| `files` | Explicit list of files or folders included in the package |
| `components` | Component list; at least one is required for installation and export |
| `homepage` | Optional homepage |
| `requires_packages` | Optional package-wide Python dependencies |
| `requires_plugins` | Optional dependencies on other plugins |

The first required fields are shown in the complete example above. You may omit `homepage` and the dependency fields.

Do not use version suffixes such as `1.0.0-dev`, `1.0.0+local`, or `1.0.0.post1`: the current installer expects final numeric versions. The `v` prefix is not part of the `version` value.

The manager may add type tags (`widget`, `workspace`, `top-bar`, `core`, `system-module`, `custom-module`) when displaying a package based on its components. Specify additional tags as lowercase English words with hyphens: `text-tools`, `productivity`, `pipeline-nodes`.

### 6.2. Compatibility

```json
{
  "api": 1,
  "python": ">=3.12",
  "platforms": ["linux", "windows"]
}
```

This is the **value of the `compatibility` field**, not a complete standalone manifest. Specify the actual requirements of your code. If you use Python 3.14 syntax, do not leave `>=3.12` simply because the template used it.

The format supports an optional `toshy` constraint, but it is meaningful only when the host build passes its version to the compatibility checker. Do not fill it with an arbitrary number: an unknown host version can also cause the plugin to be blocked.

### 6.3. Component fields

| Field | Purpose |
| --- | --- |
| `id` | Local component ID |
| `type` | `widget`, `workspace`, `topbar`, `core`, `system-module`, or `custom-module` |
| `mode` | `add`, `extend`, or `replace` |
| `entrypoint` | Python module and function: `plugin:create` |
| `path` | Folder containing module code; used instead of `entrypoint` |
| `title` | Human-readable title for this component; otherwise the plugin name is generally used |
| `placement` | Initial placement of a UI component |
| `module_name` | System name of a module component |
| `target` | Target of an extension or replacement |
| `operation` | Method of attaching to an extension point |
| `requires_packages` | Python dependencies for this component only |
| `requires_components` | Dependencies on specific components |
| `requires_contracts` | Required interfaces and their version ranges |
| `provides_contracts` | Preserved/provided interfaces and their versions |
| `group` | Related group of components within the plugin |
| `extension_points` | Extension points defined by the plugin itself |

A component must use **exactly one** of the two loading forms: `entrypoint` or `path`. `path` is allowed only for system and custom modules.

`plugin:create` means: find `plugin.py` in the package and call its `create(context)` function. For `components.status.plugin:create`, the code path is `components/status/plugin.py`.

All manifest paths are relative and use `/`, even on Windows. `C:\\...`, `/home/...`, and `../` traversal are not suitable for paths to package contents.

### 6.4. Placement

| Type | Practical `placement` values |
| --- | --- |
| `widget`, `add` mode | `left_top`, `left_bottom`, `right_top`, `right_bottom` |
| `workspace`, `add` mode | `top`, `bottom` |
| `topbar`, `add` mode | `left`, `right` |

The user can move a widget later; its saved position takes precedence over the initial placement. For `extend` with `operation: "slot"`, the location is determined by `target`, not `placement`.

## 7. Multiple components in one plugin

### Using the interface

1. Select an existing **source folder** in the top row of Export.
2. Under "Template", select the type of the next component.
3. Click "Add component…".
4. Enter a new local component ID.
5. For a system or custom module, select the folder of the prepared module and its unique system name.
6. Check the updated component list and package contents.

The plugin's ID, name, and author remain unchanged. Adding a component does not create a separate installed plugin.

New sources are placed in `components/<component_name>/`. Hyphens in folder names are replaced with underscores, so avoid a pair of IDs such as `text-tools` and `text_tools`: both would target the same folder.

"No template" cannot be added as a component in this dialog. To add a part, select a specific type.

The component list in the interface is an overview. To edit an existing component, open its files. To remove it manually, update `components`, `files`, related dependencies, and the files themselves consistently. Work from a source copy under version control.

### Example: add an indicator to the counter

Select the `author.counter` source folder, choose the "Top bar" template, and add a component with ID `status`. Open `components/status/plugin.py`:

```python
from PySide6.QtWidgets import QLabel


def create(context):
    label = QLabel("Counter connected", context.parent)
    label.setToolTip("Plugin author.counter is active")
    return label
```

You can set `placement: "right"` for the added component. The package now contains both a widget and a top-bar element. By default, the generator does not connect their states: to make the indicator show the click count, you must implement data exchange between these two UI components.

Each added module can have its own folder and `module_name`. This lets you package several custom modules and a widget in one ZIP.

Adding a component does not automatically increment the package version. If an earlier version was already installed, increase `version` before the next export.

## 8. What each template creates

| Template in the interface | What it produces | What the author needs to do |
| --- | --- | --- |
| No template | Metadata, image, license, README, empty `components` | Add at least one component and its code |
| Custom module | Packaged copy of a selected prepared module | Prepare a module with `__init__.py`; check portable settings |
| System module | Packaged copy of a selected prepared module | Prepare the module; assign a unique system name |
| Widget | A `QLabel` as a basic `QWidget` added to a sidebar | Replace it with your own interface |
| Workspace | A `QLabel` as the contents of a new workspace tab | Create the required `QWidget`; change placement if needed |
| Top bar | A simple `QWidget` added to the top panel | Implement a button, indicator, or other compact element |
| Core | A `create(context)` function that initially returns `None` | Implement registration/an object, or manually describe the required extension |
| Pipeline nodes | A system module with two sample operations and argument schemas | Adapt the operations, names, schemas, and implementation |

### "No template"

This is a complete source folder but not yet an installable plugin. While `components` is empty, "Inspect package", export, and installation must reject it and request a component. An empty scaffold is useful for a complex package you want to assemble yourself.

### Modules

The current module-template interface **packages a module that has already been prepared**; it does not open a tool-development wizard. The dialog asks you to choose the module's folder. A minimal preparation example is in the next chapter.

When creating a new package, the source folder's name becomes the system module name. Therefore, name it as a valid Python identifier in advance, for example `alex_text_tools`. When adding a module to an existing plugin, the interface asks for this name separately.

UI templates do not automatically become interfaces for a module. If a widget must control a core module, you need to implement communication through the application's bridge; merely having both components in the ZIP only installs them together.

### "Pipeline nodes"

This is a separate convenience template, not a seventh component type in `plugin.json`. It creates `type: "system-module"` with the additional tag `pipeline-nodes`. The editor discovers the module's operations as nodes.

## 9. A module with a tool for the agent

A module provides agent functionality. A widget provides a user interface. You do not need to create a widget for every new tool.

### 9.1. Prepare the module source folder

Create a separate `author_text_tools` folder and place an `__init__.py` file inside it:

```python
from core.registry import BaseModule, BaseTool


class UppercaseTool(BaseTool):
    pipeline_title = "Text to uppercase"

    def __init__(self):
        super().__init__(
            name="author_text_uppercase",
            description="Convert the provided text to uppercase.",
            args_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Input text"}
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        )

    async def execute(self, text):
        return text.upper()


class Module(BaseModule):
    def get_tools(self):
        return [UppercaseTool()]
```

The name `Module` matters: it is how Toshy obtains an instance of the module's contents. `get_tools()` returns the tools. A tool's `name` is its stable technical identifier, `description` explains its purpose to the model, `args_schema` describes its inputs, and `execute` performs the action.

`args_schema` is JSON Schema. `string`, `integer`, `number`, `boolean`, `array`, and `object` specify types; `required` lists mandatory fields. Schema arguments must match the parameters of `execute`.

For a readable module name in the editor, you can create `ui/config.json` inside the module source folder:

```json
{
  "display_name": "Text tools",
  "color": "#34b4a1"
}
```

### 9.2. Package the module

In Export, select "Custom module" or "System module", fill out the new plugin's details, click "Create source…", and choose the prepared `author_text_tools` folder when prompted.

The generator copies the module into the package. The component will look approximately like this:

```json
{
  "id": "main",
  "type": "custom-module",
  "mode": "add",
  "module_name": "author_text_tools",
  "path": "module"
}
```

This is **an element of the `components` array**. The `files` list must include the `module` folder. In the system variant, `type` changes to `system-module`.

After copying, edit the module **inside the plugin source folder**. Changes made to the original external folder will not automatically be copied into the package.

### 9.3. Make the tool available

Export and install the package, enable the plugin, and fully restart Toshy. Then enable the added module in the core configuration and allow it for the required Project Folder under the application's usual rules. Select an appropriate session and start the core.

Only then should you check whether the model can call `author_text_uppercase`. The plugin may be installed correctly, but its tool will not be available to a project that has not been granted access to the module.

### 9.4. Additional module lifecycle

`BaseModule` provides initialization and resource-release methods, including `async_init`, `deactivate`, and `shutdown`, as well as notifications about changes to the module set. They are unnecessary for this example, which has no external resources.

If a module opens a connection or starts a process, use these methods appropriately: disabling the module and shutting down the core must release those resources. Do not start heavy services merely by importing `__init__.py`.

## 10. Pipeline nodes

To create a set of executable nodes, select "Pipeline nodes". The scaffold contains two examples: converting text to uppercase and adding numbers.

Their code is in `module/__init__.py` or the corresponding nested component folder. The system name and operation names are generated with a unique prefix. Avoid renaming them after publication unless necessary: saved graphs refer to technical identifiers.

### How an operation becomes a node

- The class inherits from `BaseTool`.
- `super().__init__` specifies `name`, `description`, and `args_schema`.
- `Module.get_tools()` returns an instance of the class.
- The editor creates typed Arg inputs from `args_schema.properties`.
- The node has an Exec input and output, a Prompt input, and a `result` output.
- `pipeline_title` specifies the readable card title.

The Prompt input belongs to the node's standard execution mechanism; you do not need to add it as an argument to `execute(prompt=...)` unless the operation itself declares such an argument. With all inputs provided, a deterministic operation can execute directly without asking the model.

The property `model_visible = False` in the examples means that the operation should not be offered to the model as an ordinary tool. It does not prevent direct node execution. If you want both a node and a model-accessible tool, remove this flag or set it to `True`.

### Discovery with the core turned off

The editor reads metadata from `__init__.py` using AST without executing module code. Therefore, write `name` and `args_schema` **as literal values directly** in the constructor call, as in the previous chapter's example.

`name=build_name()` or `args_schema=SCHEMA` may work when the Python code executes, but the current static analyzer does not evaluate these expressions. Likewise, classes defined only in other files are not automatically discovered by this simple scan of `__init__.py`. For a complex implementation, leave adapter classes with literal metadata in `__init__.py` and move the heavy logic to adjacent files.

Static discovery does not validate all executable code or prove that a node works. After a full restart, the application can show nodes even with the core off; to run them, the module itself must be enabled and allowed for the project.

In the editor library, look under `System modules → <module name>`. Test the individual operation, not only the entire module card: the module card represents a different execution level.

The current template uses the standard `result` output. It does not create an arbitrary collection of named output ports. Returning a dictionary does not automatically turn its keys into new node outputs.

## 11. Context API and lifecycle

### 11.1. Factory function

For a component with an `entrypoint`, Toshy calls the specified function:

```python
def create(context):
    # Create an object or handler of the required type.
    return None
```

This snippet shows only the signature. Returning `None` is permitted, for example, for a `core/add` component performing registration. A UI addition requires a `QWidget`, while an event extension requires a callable handler. Returning the wrong type blocks the component.

The factory must be synchronous. Do not turn `create` into an `async def`. Handlers for relevant core events/operations and module methods may be asynchronous, but creating a Qt widget must not be.

### 11.2. What is in context

| Property/method | Provides |
| --- | --- |
| `context.plugin_id` | ID of the entire plugin |
| `context.component_id` | Full component key, such as `author.counter/main` |
| `context.plugin_dir` | `Path` to the selected installed version's package folder |
| `context.data_dir` | `Path` to a separate plugin user-data folder, created on access |
| `context.host` | Host object for this attachment |
| `context.app` | Alias for `host`; not automatically a `QApplication` |
| `context.parent` | Parent Qt element during UI creation; may be absent in the core |
| `context.default_factory` | Creates the original component during a supported replacement |
| `context.data` | Additional data for a specific attachment; its type depends on the extension point |
| `context.runtime` | Plugin runtime for the current process |
| `context.register_cleanup(callback)` | Registers resource cleanup for the component |

Context depends on where the function is called. For a UI addition, `host` is usually the main window; in core factories and preparation handlers, it is the module registry. Do not assume that `context.host` always contains `central_stack` or a chat interface.

When extending `ui.chat.message.actions`, `data` contains a dictionary with `turn` and `kind`. For `ui.chat.input.actions`, it contains the current chat view object. Additional data may be absent when creating top-bar slots. Account for this before accessing attributes.

### 11.3. When code is loaded

- Plugin code is not executed when checking package contents or extracting a ZIP.
- After a restart, preparation loads factories for event and operation handlers.
- UI-slot factories are called when the host populates the relevant area. For example, a message button is created for a particular message; the factory may be called many times.
- Independent UI additions are created while building the interface.
- `core/add` is prepared in the core process.
- Modules are attached through the module registry and their lifecycle.

Do not start actions with external side effects at the top level of an imported file. Importing may be necessary during component preparation, before any user action.

The UI and core do not share the same Python memory. A global variable changed by a widget is not a way to send a command to a core module. For communication, study the existing `ui/core_bridge/` and use the application's corresponding operations. The mere existence of `PluginContext` does not provide a universal RPC method.

### 11.4. Cleanup

Subscriptions, timers, open files, background jobs, and processes must be shut down correctly. Use `context.register_cleanup` for factory-owned resources.

An example of **replacing the body of a UI component factory**: a timer-updated indicator.

```python
from PySide6.QtCore import QTimer, QTime
from PySide6.QtWidgets import QLabel


def create(context):
    label = QLabel(context.parent)
    timer = QTimer(label)

    def update_clock():
        label.setText(QTime.currentTime().toString("HH:mm"))

    timer.timeout.connect(update_clock)
    timer.start(1000)
    update_clock()
    context.register_cleanup(timer.stop)
    return label
```

Include it in `widget/add`, `topbar/add`, or an appropriate UI slot. The timer uses `label` as its parent. Cleanup callbacks run in reverse registration order; do not expect that order to change when enabling or disabling a card during the current application session.

If `core/add` returns an object with a `close()` method, the runtime registers its closure automatically. Do not register the same `close` a second time without a reason. For network/asynchronous resources, account for event-loop availability and the core lifecycle.

### 11.5. Threads

Create and modify Qt widgets in the GUI thread. Heavy calculations, network waits, and large file reads must not block the interface. Return background-task results using a thread-safe signal/bridge mechanism.

`async def` does not, by itself, move computation to another CPU core. Do not run an endless synchronous calculation inside an asynchronous core handler. A separate process may be appropriate for heavy isolated computation, but implementing it is the plugin's responsibility.

## 12. Extending existing functionality

There are three distinct actions:

| Mode | Meaning | Example |
| --- | --- | --- |
| `add` | Add an independent component | New widget |
| `extend` | Attach to a declared extension point | Additional chat button |
| `replace` | Supply an alternative implementation of an existing component | Custom version of a built-in panel |

In the current interface, the basic templates create `add` components. For `extend` and `replace`, edit the component in `plugin.json` and implement the appropriate factory. Choosing the "Core" template does not, by itself, intercept every agent action.

### 12.1. Available built-in extension points

| `target` | Kind | `operation` | Data |
| --- | --- | --- | --- |
| `ui.topbar.left` | UI slot | `slot` | Additional data may be absent |
| `ui.topbar.right` | UI slot | `slot` | Additional data may be absent |
| `ui.chat.input.actions` | UI slot | `slot` | Chat view object in `context.data` |
| `ui.chat.message.actions` | UI slot | `slot` | `context.data`: `turn`, `kind` |
| `ui.ready` | UI event | `listen` | `payload`: `window` |
| `ui.workspace.changed` | UI event | `listen` | `payload`: `index`, `widget`; when the upper workspace view changes |
| `core.ready` | Core event | `listen` | `payload`: `registry` |
| `core.run_events` | Run events | `listen` | `payload`: `event`, `context`; streamed `content` and `thought` are excluded here |
| `core.tool.execute` | Core operation | `before`, `around`, `filter`, `after` | `payload`: `name`, `args`, `context` |

These are specific contracts of the current implementation. Having a similar object somewhere in the application does not automatically create a new `target`. For example, you cannot simply write `ui.file_manager.after_button` unless the host declares and calls that extension point.

### 12.2. Example: a button in the top panel

The following JSON is **a component in the `components` array**, not a complete file:

```json
{
  "id": "hello-button",
  "type": "topbar",
  "mode": "extend",
  "target": "ui.topbar.right",
  "operation": "slot",
  "entrypoint": "hello:create"
}
```

Create `hello.py` in the source root and include it in `files`:

```python
from PySide6.QtWidgets import QPushButton, QMessageBox


def create(context):
    button = QPushButton("Hello", context.parent)

    def show_message():
        QMessageBox.information(button, "My plugin", "Plugin connected")

    button.clicked.connect(show_message)
    return button
```

A slot factory returns the `QWidget` itself, not a callback function. Multiple independent buttons can be added to the same area.

The `ui.ready` event is suitable for actions after the main interface is created, but it does not turn arbitrary access to internal objects into a stable API. If you manually rearrange buttons by accessing private window attributes, such an integration needs to be checked again after Toshy updates.

### 12.3. Example: observing workspace changes

An element of `components`:

```json
{
  "id": "workspace-observer",
  "type": "workspace",
  "mode": "extend",
  "target": "ui.workspace.changed",
  "operation": "listen",
  "entrypoint": "observer:create"
}
```

The `observer.py` file, which must also be included in `files`:

```python
import logging


def create(context):
    logger = logging.getLogger("author.plugin.workspace")

    def on_workspace_changed(payload):
        logger.info("Upper workspace changed: %s", payload["index"])

    return on_workspace_changed
```

Here, the factory returns a handler. The manager subscribes it to the declared event. The standard Python logger in the example uses the host's configuration; do not open a separate global log file on every event.

UI handlers must be synchronous. A handler must not alter the agent's main system message or touch data unrelated to its function.

### 12.4. Core operation hooks

An operation differs from an event: it has an action to execute and a result. For `core.tool.execute`, the following are available:

| Operation | Handler signature | Role |
| --- | --- | --- |
| `before` | `handler(payload)` | Perform an action before the chain; the return value does not replace the request |
| `around` | `handler(next_call, payload)` | Wrap subsequent execution; return its result |
| `filter` | `handler(result, payload)` | Return a transformed result |
| `after` | `handler(result, payload)` | Observe a successful result; the return value is ignored |

Core handlers may be asynchronous. The factory returning them remains synchronous.

Example of a component that measures execution duration:

```json
{
  "id": "tool-timing",
  "type": "core",
  "mode": "extend",
  "target": "core.tool.execute",
  "operation": "around",
  "entrypoint": "timing:create"
}
```

The `timing.py` file, included in `files`:

```python
from time import perf_counter


def create(context):
    from core.logger import core_logger

    async def measure(next_call, payload):
        started = perf_counter()
        try:
            return await next_call(payload)
        finally:
            elapsed = perf_counter() - started
            core_logger.event(
                "app",
                "PLUGIN_EVENT",
                f"{context.plugin_id}: {payload['name']} — {elapsed:.3f} s",
                level="INFO",
            )

    return measure
```

`next_call` is the continuation of the chain. It may be called **at most once**. If an error occurs after a tool has executed, do not automatically retry the action: it may already have changed a file, sent a request, or created an object.

`before`/`after` are managed outside the `around` chain. Filters are applied so that the higher-priority one gets the final transformation. A successful tool result must not become an error merely because an `after` observer fails.

Do not substitute a tool in `payload` that is unavailable to the current run. Ordinary calls use the permitted tool set. Full arguments and context can contain sensitive information — avoid writing them to logs unnecessarily.

### 12.5. Custom extension points

A complex plugin can declare a point that other plugins can attach to. Its name must be within the plugin's namespace:

- `ui.plugin.<plugin_ID>.<name>`;
- `core.plugin.<plugin_ID>.<name>`.

For example, a fragment of an **existing component** in `author.counter`:

```json
{
  "extension_points": {
    "ui.plugin.author.counter.changed": {
      "strategy": "event",
      "version": "1.0"
    }
  }
}
```

All other required component fields remain unchanged. When the value changes, your UI code can call:

```python
context.runtime.emit(
    "ui.plugin.author.counter.changed",
    {"value": 10},
)
```

This is a fragment of code where `context` is available; it is not a standalone script. Declaring an event in the manifest does not generate calls to it — you must write those yourself. Operations and slots likewise require actual attachment in code. These events belong to a specific process and are not an automatic UI ↔ core channel.

## 13. Replacing built-in components

### 13.1. When to use replace

If you need to add a button to an existing location, start with `extend`. If you need an independent tool, start with a module. Use `replace` when you genuinely provide an alternative implementation of an existing component.

Main targets declared by the current host:

| Category | Targets |
| --- | --- |
| Workspaces | `workspace.chat`, `workspace.projects`, `workspace.files`, `workspace.board`, `workspace.paint`, `workspace.startup`, `workspace.graph`, `workspace.pipelines`, `workspace.module`, `workspace.log` |
| Side panels | `widget.file_manager`, `widget.sessions`, `widget.tasks`, `widget.subagents`, `widget.personality`, `widget.mcp`, `widget.terminal`, `widget.web_search` |
| Top bar | `topbar.main` |
| Agent | `core.agent` |
| System module | `module.system.<system_name>` |
| Custom module | `module.custom.<system_name>` |

The specific target must be registered and actually used by the host in your build. A list of targets does not mean that any fragment of any script can be replaced by its string name.

### 13.2. UI replacement contract

To replace a built-in Qt component, returning an arbitrary `QWidget` is not enough: the object must be an instance of the original class or its subclass and preserve the required interface.

For example, replacing `widget.tasks` must preserve the `TasksPanel` contract, including the sidebar infrastructure. Replacing Chat must preserve the fields and signals used by the application. Removing internal methods used by other code can break the application even if the replacement passes basic validation.

A safe first experiment is to create the original component using `default_factory()` and make a limited styling change.

An element of `components`:

```json
{
  "id": "tasks-style",
  "type": "widget",
  "mode": "replace",
  "target": "widget.tasks",
  "entrypoint": "tasks_style:create"
}
```

The `tasks_style.py` file, included in `files`:

```python
def create(context):
    panel = context.default_factory()
    panel.setToolTip("Tasks panel customized by a plugin")
    return panel
```

This is a minimal example of the replacement mechanism, not a complete redesign. For your own interface, study the original class and create a compatible subclass. Keep it in your package; do not replace `ui/panels/tasks_panel.py` on disk.

If the selected replacement fails while building the UI, the host attempts to create the built-in version. This is not a reason to rely on automatic correction of arbitrary plugin behavior; the runtime does not automatically activate another third-party replacement midway through execution.

### 13.3. Preserving child extension points

Some components expose interfaces to other plugins. Built-in Chat provides `ui.chat.input.actions` and `ui.chat.message.actions`; the top panel provides `ui.topbar.left` and `ui.topbar.right`; the agent provides `core.run_events`.

When replacing a parent component, do not silently assume that its child interfaces will continue to work. If you preserve them, declare the actual versions in `provides_contracts` and really invoke the corresponding mechanisms in your code.

Fragment of a Chat replacement component:

```json
{
  "provides_contracts": {
    "ui.chat.input.actions": "1.0",
    "ui.chat.message.actions": "1.0"
  }
}
```

This is a compatibility declaration, not an implementation of the buttons. If you declare a contract without providing it, the manager cannot prove from your JSON alone that your logic is incorrect.

A complete replacement of `core.agent` requires knowledge of the existing agent interface. For a first plugin, use a module or `core.tool.execute`; an empty generic object is not a suitable replacement for the agent.

## 14. Dependencies, priorities, and conflicts

### 14.1. What priority does

If two enabled plugins compete for the same exclusive replacement, the eligible component from the higher-priority plugin is selected. Both participants may receive a warning. Unrelated components continue to work as long as their dependencies remain satisfied.

Priority does not mean that "the entire upper plugin erases the entire lower plugin". For independent additions, it determines participation order; for replacements, it selects an implementation; for filters, it determines transformation order.

The order and switches take effect after restarting the application. Dragging a card should not cause the manager to change the current agent run's chain.

### 14.2. What priority cannot solve

- It does not merge arbitrary Python function bodies.
- It does not verify the meaning of every modification made by a plugin.
- It does not create isolated environments for two incompatible library versions within one process.
- It does not guarantee compatibility for manual monkey patching or access to private attributes.

Where possible, use narrowly scoped extension points and declare dependencies. Direct access to application objects is technically possible, but the manager cannot reliably detect all consequences of such interference.

### 14.3. Python dependencies

Example of a package-level field:

```json
{
  "requires_packages": ["httpx>=0.27,<1"]
}
```

This is a manifest fragment, not a complete package. Dependencies are checked in Toshy's environment. **Automatic installation is disabled.** Importing a ZIP does not run `pip`, create a separate `venv`, or execute an installation script.

A missing-package message means you need to install it in the environment actually used to launch Toshy. The command takes this form:

```bash
python -m pip install "httpx>=0.27,<1"
```

Here, `python` is the specific interpreter of Toshy's environment. On your system, the command may be named `python3` or point to a `venv`. Do not install dependencies into an arbitrary system Python or replace Toshy's Qt libraries without checking compatibility.

The current dependency format supports names and version ranges but not URL dependencies or extras such as `package[feature]`. Environment markers are supported where platform-specific differences require them.

If a library is needed by only one optional component, declare `requires_packages` on that component. A package-level dependency blocks the entire plugin; a component-level dependency blocks only that part and parts that depend on it.

### 14.4. Dependency on another plugin

Fragment at the root of `plugin.json`:

```json
{
  "requires_plugins": {
    "other.shared-tools": ">=1.0,<2"
  }
}
```

The other plugin must be installed, enabled, and satisfy the version requirement. Simply having its folder is not enough.

### 14.5. Dependency on a component and interface

Component fragment:

```json
{
  "requires_components": ["author.counter/main"],
  "requires_contracts": {
    "ui.topbar.right": ">=1,<2"
  }
}
```

`requires_components` requires a particular component to be working. `requires_contracts` requires an interface of a particular version, regardless of who provides it. Such a dependency does not implement data exchange or transfer objects between processes.

`group` ties related parts of one package together: if one is unavailable, the manager excludes the entire related group. Use this for parts that do not make sense on their own. Do not create cyclic dependencies.

Package, API, and contract versions are separate quantities. A plugin at version `2.3.0` may use API `1` and provide its own interface at version `1.0`.

## 15. Files, data, and portability

### 15.1. What gets included in the ZIP

`files` is an explicit allowlist of contents. If you created `utils.py` but did not add it or its containing folder to `files`, the exporter will not include it.

For example, for a relative import from an adjacent file:

```python
from .utils import format_value
```

both source files must be included in the package. Inside a nested folder, use relative imports as you would in a normal Python package. Toshy loads the entry point under its own package name; `import utils` may find an unrelated module or fail.

`plugin.json` is included automatically. Do not add it as an item in `files`. The `LICENSE` file and image must exist and be included. Paths to entry points and module contents are also validated.

### 15.2. What is excluded

Normal exports filter out, among others:

- `.git`, `.venv`, `venv`, `env`, `__pycache__`, `node_modules`;
- the folders `logs`, `sessions`, `runtime`;
- `.env`, `.env.*`, files named `auth.json`, `credentials.json`, `secrets.json`, `oauth_tokens.json`;
- `.pyc`, `.pyo`, `.log`, `.pem`, `.key`;
- service files prefixed with `.toshy-`.

Do not put a static folder needed by users under a reserved excluded name such as `runtime`: it will not be packaged. Check the actual export file list.

The filename filter does not search for passwords inside Python code, images, or arbitrarily named JSON files. Review the included files yourself.

For copied MCP modules, the generator clears machine-specific launch settings, local environment values, and credentials from standard server descriptions; a locally installed dependency environment is not transferred as a ready-made `venv`. After installation on another machine, the server may need to be configured and installed again.

### 15.3. Where to store user data

Installed code has separate version folders. Writing settings next to `plugin.py` ties the data to a particular version and makes updates difficult.

For factories with a context, use `context.data_dir`. For example, **inside `create(context)`**, after creating the counter widget:

```python
import json

state_file = context.data_dir / "counter.json"
try:
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    value = saved.get("count", 0)
    widget.count = value if isinstance(value, int) and value >= 0 else 0
except (OSError, ValueError, AttributeError):
    widget.count = 0
widget.label.setText(f"Clicks: {widget.count}")


def save_state():
    state_file.write_text(
        json.dumps({"count": widget.count}, ensure_ascii=False),
        encoding="utf-8",
    )


context.register_cleanup(save_state)
```

This fragment assumes `widget = CounterWidget(context.parent)` comes before it and `return widget` comes afterward. Data is saved during normal cleanup; the most recent value may be lost on a crash. For important data, implement periodic/event-based saving and atomic writes.

`data_dir` is shared by the plugin's versions, so plan a migration when changing the data format. Removing code through the manager preserves user data; it is not equivalent to erasing every trace of the plugin.

For `path`-based modules, the `Module()` object is created without `PluginContext`. Do not add a required `context` parameter to its constructor expecting the registry to provide one. For complex initialization, use the existing module lifecycle or deliberately implement the component through an `entrypoint`.

### 15.4. Windows and Linux

- Use `pathlib.Path`; do not build paths by concatenating strings with `\\` or `/`.
- Keep portable relative paths with `/` in the manifest.
- Avoid filenames that differ only in case: `Widget.py` and `widget.py` conflict on typical Windows installations.
- Do not use `CON`, `NUL`, `PRN`, `COM1`, and other reserved Windows names, trailing periods/spaces, or forbidden characters.
- Do not distribute symbolic links or special files.
- Do not assume Bash, `/bin/sh`, `apt`, `dnf`, or PowerShell is available on every platform.
- Do not hard-code absolute paths from your own machine.
- If only one OS is supported, state it accurately in `compatibility.platforms`.

### 15.5. Format limits

| Limit | Value |
| --- | --- |
| Format version / API | `1` / `1` |
| Components per package | Up to 128 |
| `files` entries | Up to 256 |
| Files/archive entries | Up to 4096 |
| Total extracted size | Up to 256 MiB |
| ZIP size on installation | Up to 256 MiB |
| One ordinary file | Up to 32 MiB |
| `plugin.json` | Up to 128 KiB |
| Plugin image | Up to 2 MiB |

Do not use a plugin package to transfer an enormous environment or model weights. Downloading external resources when necessary is separate functionality that you must implement and explain to the user.

## 16. Export, installation, and releasing an update

### 16.1. Checking contents is not testing

"Inspect package" validates the manifest, paths, declared file existence, and packaging limits. It does not call `create`, run a tool, open a network connection, or guarantee the absence of Python runtime errors.

After a successful check, "Export ZIP…" becomes available. If included source files change between validation and export, the application asks you to inspect the package again. This is normal protection against exporting different contents.

### 16.2. Three separate copies

| Copy | Purpose | What to edit |
| --- | --- | --- |
| Source folder | Development, Git, metadata | Change code here |
| Exported ZIP | Distribution and installation | Do not edit manually; rebuild from sources |
| Installed version folder | Executing a selected version | Do not use as a development working folder |

Editing the sources without reinstalling does not update the installed copy. Simply copying a source folder next to `plugins/` does not register the package with the manager either.

### 16.3. Local development cycle

1. Change code in the source folder.
2. If this ID/version has already been installed, increment the version in `plugin.json`.
3. Make sure new files are included in `files`.
4. Click "Inspect package" and review the list.
5. Export the ZIP outside the source folder.
6. Import it via "Library → Import ZIP…".
7. Check the switch; a new plugin is disabled by default, while an update preserves its state.
8. Fully restart the application.
9. Test the feature and review warnings.

The current mechanism does not promise hot reload of Python code. Restarting the core does not replace a full UI restart after plugin changes.

### 16.4. ZIP structure

`plugin.json` must be at the root, next to the declared files. An extra outer `my-plugin/` folder containing the entire package is not suitable for direct installation from such an archive.

Use the built-in exporter to get the correct structure. A GitHub branch archive containing source code and an installable Toshy package are different artifacts.

### 16.5. Removal and recovery

To remove an active plugin, disable it and fully restart Toshy, then select "Remove plugin…". The current application session may still hold the earlier package, so removal of active code is restricted.

If the interface no longer lets you open the manager after development, quit Toshy and back up `plugins/state.json`. In the current storage structure, the `enabled` list contains IDs of enabled plugins. Removing the problematic ID from **that list** with the application completely stopped disables it on the next start. Preserve valid JSON and the other fields; do not randomly delete folders or version records instead of diagnosing the problem.

To return to a known working version, you can import its saved ZIP. Directly importing an older version triggers a warning. Rolling back code does not roll back user data; design data migrations separately.

## 17. Publishing on GitHub and in the registry

### 17.1. Direct import from a public release

To distribute without a shared catalog:

1. Create your own repository containing the source code, `README.md`, and license.
2. Export a working Toshy package.
3. Create a GitHub Release for that version.
4. Attach the **ready-made plugin ZIP as a release asset**.
5. Give users a link to the repository, a specific release, or the ZIP file in the release.

The user opens "Library → Import from GitHub…". Connecting a GitHub account to Toshy is not required for public packages. Network access and GitHub availability are still required.

Supported link formats, where `OWNER` and `REPOSITORY` are replaced with real values:

```text
https://github.com/OWNER/REPOSITORY
https://github.com/OWNER/REPOSITORY/releases/latest
https://github.com/OWNER/REPOSITORY/releases/tag/1.0.0
https://github.com/OWNER/REPOSITORY/releases/download/1.0.0/author.counter.zip
```

For a repository-only link, the normal latest release is used. To specify a version, provide a link to its tag/file. If there are several ZIPs, the application will offer a package selector. Avoid attaching multiple ambiguously named ZIPs when users will have difficulty telling them apart.

If the release has no ready-made ZIP, import does not build the Python project for the author or automatically install dependencies. GitHub's automatic "Source code (zip)" links are not a substitute for the prepared package.

### 17.2. Shared catalog

A catalog and an individual plugin repository serve different purposes. The catalog contains a `catalog.json` with package information and links to releases. The "Plugin catalog repository" field is intended for such a catalog.

In addition to the name, ID, version, description, author, license, compatibility, and tags, a catalog entry includes:

- a link to the release image;
- an `archive.url` link to the ZIP in the release;
- `archive.size`, its size in bytes;
- the ZIP's SHA-256 in `archive.sha256`;
- a `source` link to the corresponding source code;
- `source_sha256`, the fingerprint of the package's declared contents.

`source_sha256` is not a Git commit SHA, nor is it necessarily the ZIP's SHA. It is computed by the built-in packager's `source_digest` function from the names and hashes of the included files. You do not need to prepare a catalog for a first direct import from GitHub.

To be listed in a shared catalog, an author submits changes manually through a Pull Request under the catalog owner's rules. Exporting from Toshy does not create a repository, publish a release, or submit a request on your behalf. Catalog validation does not change the fact that the user is installing third-party code.

Catalog updates are performed when the user clicks the corresponding action. If downloading or installing fails, the previously installed version should remain available. Do not confuse the integrity of a downloaded package with a guarantee that its behavior is safe.

## 18. How to test and debug a plugin

### 18.1. Packaging checks

- `plugin.json` parses as JSON and passes "Inspect package".
- All required files are shown, including new Python modules and resources.
- There are no secrets, caches, user sessions, or logs.
- The ZIP imports into a clean test copy of Toshy.
- Changed code is released under a new version.

### 18.2. UI checks

- The plugin appears after a full restart.
- The widget/workspace is placed where expected.
- Buttons, signals, keyboard interaction, and window sizing work.
- Recreating UI parts does not duplicate global subscriptions or timers.
- Heavy operations do not block the GUI.
- Disabling the plugin and restarting restores normal behavior.

A message UI slot is invoked for many messages. Do not open a new global server connection or create an application-wide subscription in every such factory without managing its lifecycle.

### 18.3. Module and node checks

- The module appears in the correct category after plugin installation.
- It is enabled in the core configuration and permitted for the project.
- Tool names are unique.
- A call with valid arguments returns the expected result.
- Invalid arguments and external service errors are handled clearly.
- Nodes are visible with the core off if the static analyzer can read their metadata.
- Resources are released when the core stops or a module is disabled.

### 18.4. Compatibility checks

- Without any third-party plugins.
- With ordinary extensions in the same UI area.
- With an alternative replacement for the same target above and below it in the list.
- When a required dependency is missing.
- After updating from the previous version with saved data.
- On both declared operating systems, or with an accurate platform restriction.

Conflict testing is useful even if you are the only author: deliberately create two small `replace` components targeting the same point in a test environment and verify that the warning and selected implementation match expectations.

### 18.5. Where to look for the cause

1. In the "Export" or "Library" message after an operation.
2. In the tooltip on the installed package's yellow warning icon.
3. In the difference between planned state and "current" state — a restart may be required.
4. In the application/core log; plugin system errors are marked `PLUGIN_EVENT`.
5. In the console traceback if you launch Toshy from a terminal in your environment.

For a local launch from the project root, use `python run_ui.py` with the correct interpreter. Do not run a second instance against the same working data unnecessarily; use a separate test copy.

## 19. Common mistakes

| Symptom | Likely cause and action |
| --- | --- |
| I changed "Name" in the form, but the package kept its old name | The field is used during creation; change `name` in the selected `plugin.json` |
| I selected a different template, but the code did not change | Selecting a template does not overwrite anything; use the create or add-component button |
| "No template" fails validation | `components` is empty; add an actual component |
| "Export ZIP…" is disabled | Select the correct folder and successfully inspect the package |
| It asks to check contents again | Included sources changed after validation |
| `A root plugin.json manifest is required` | The ZIP has an extra outer folder or is not a Toshy package at all |
| `Entrypoint source is missing` | Incorrect `entrypoint`, or the source file is not included in `files` |
| A file exists in the sources but not in the ZIP | It is not declared in `files` or was excluded by a filter |
| Error about an existing version with different bytes | Increment `version` and export again |
| I installed the ZIP but nothing appeared | A new package is disabled; enable it and fully restart the application |
| I restarted the core but the UI is still old | A full Toshy restart is required |
| `No module named ...` for my own file | Check `files` and relative imports within the package |
| A Python dependency is missing | Install it in Toshy's environment; importing the package does not run `pip` |
| The plugin works only partially | Review warnings for individual components and their dependencies |
| The widget works but the tool is unavailable to the model | Check the module, project settings, and `model_visible` |
| A node is not visible when the core is off | Check literal metadata in `__init__.py`, `Module.get_tools()`, and perform a full restart |
| A node is visible but fails when executed | Static analysis does not check implementation; inspect the module, arguments, and traceback |
| Two versions of the same library cannot coexist | Plugin priority does not isolate Python dependencies |
| A replacement is blocked | Unknown target, lost contract, dependency, priority, or incorrect result class |
| "Add component…" is unavailable for an installed version | The selected folder is managed and contains `.toshy-receipt.json`; use the source project |
| No package is found when importing from GitHub | A published release must contain a ready-made ZIP, not just source code |
| Windows rejects a package that works on Linux | Check capitalization, reserved names, characters, and portable paths |

## 20. Where to look in Toshy's source code

You do not need to memorize the structure of the entire agent. These files help locate a specific contract when the basic examples are no longer enough:

| File/folder relative to Toshy root | What to look for |
| --- | --- |
| `core/plugins/manifest.py` | Allowed fields, JSON schemas, ID rules, compatibility |
| `core/plugins/contracts.py` | Built-in targets and permitted extension operations |
| `core/plugins/runtime.py` | `PluginContext`, factories, events, chains, cleanup |
| `core/plugins/resolver.py` | Dependencies, groups, conflicts, and priorities |
| `core/plugins/package.py` | ZIP contents, limits, exclusions, export |
| `core/plugins/store.py` | Version installation, state, removal |
| `core/plugins/authoring.py` | Exactly what templates and component creation generate |
| `core/plugins/node_template.py` | Pipeline node template code |
| `core/plugins/modules.py` | Attaching packaged modules without copying into application sources |
| `core/registry.py` | `BaseModule`, `BaseTool`, tool registration and invocation |
| `ui/plugin_host.py` | Creating UI additions, slots, and validating replacements |
| `ui/plugin_manager_pages.py` | Export fields and actions |
| `ui/plugin_authoring.py` | Template list and multi-component UI |
| `ui/plugin_library.py` | ZIP/GitHub import and Library |
| `core/plugins/catalog.py` | Loading the catalog and prepared release packages |
| `ui/core_bridge/` | Communication between UI and the separate core process |
| `ui/views/modules/nodes/providers/modules.py` | Static discovery of node operations and schemas |
| `ui/panels/`, `ui/views/` | Contracts for specific built-in panels or workspaces in complex replacements |

Start with the file defining the interface you need and then find where it is called. For example, before using `context.data`, inspect the specific call to `populate_slot`, and before replacing a component, check the corresponding `build_widget` or runtime invocation in the core.

### Actual storage

In the structure checked, the storage root is `plugins/` inside the application root:

| Path | Role |
| --- | --- |
| `plugins/packages/<plugin-id>/<version>/` | Installed code for a version |
| `plugins/data/<plugin-id>/` | Separate plugin user data |
| `plugins/state.json` | Selected versions, ordering, and enabled state |
| `plugins/cache/` | Library cache |
| `plugins/runtime/` | Configuration snapshots for the current launch |

This description is for orientation and recovery, not an invitation to manage the plugin system by hand. Normal installation and updating are performed through the interface.

## 21. Pre-publication checklist

1. The package has a unique stable ID and a new version for any changed release.
2. The name and description explain what users will get.
3. The selected template has been completed: the required functionality is implemented instead of a placeholder.
4. All components are declared in `plugin.json`; supporting files are included in `files`.
5. Module and tool names use their own prefixes.
6. Declared dependencies and operating systems reflect reality.
7. Application code is not overwritten, and working data is stored separately from the installed version.
8. Subscriptions, processes, and other resources have correct cleanup.
9. Replacements preserve the required classes, interfaces, and actual child extension points.
10. The license is stated and required licenses for included third-party code are preserved. The template creates MIT for a new scaffold; when copying a prepared module with a `LICENSE`, it carries over that license. Check that the final `LICENSE` and `license` accurately describe the package contents.
11. Files have been checked for secrets and local absolute paths.
12. Import, enablement, full restart, functional testing, disabling, and updating have been tested in a separate Toshy copy.
13. For GitHub, the release contains the actual installable ZIP, and the README explains configuration and limitations.

### If you remember just one workflow

**Create source → write code → check `plugin.json` and contents → export ZIP → import via Library → enable → fully restart → test the functionality.**

For the next release: modify the sources, increase the version, and repeat this workflow starting with inspecting the package. A multi-component plugin follows the same process: the number of components does not change how it is installed.


### Release installation and compatibility (Toshy 0.1.0, API 1)

Managed installations keep immutable code under a version directory and mutable
application data under a separate root. `APP_PATHS.code_root` identifies application
code; `APP_PATHS.root` identifies its data in a managed installation. Plugin code
stays in `context.plugin_dir`; write settings, databases and downloaded dependencies
to `context.data_dir`. Never modify an installed application or plugin package.

For Qt organization/application settings use `from ui.settings import QSettings`,
or an explicit INI filename under `context.data_dir`. Direct native QSettings on
Windows can write to the registry outside the installation's data directory.

`compatibility.toshy` is checked against the real application version.
`compatibility.architectures` may declare `["x86_64"]` or `["aarch64"]`; omit it
only for genuinely architecture-independent code. The first supported Toshy
release targets Windows/Linux x86-64. Declare native dependencies accordingly.

The optional manifest integer `data_schema` defaults to 1. Keep the same schema
only while existing data remains compatible. Automatic plugin updates reject a
schema change until an explicit migration is supplied; they do not run arbitrary
migration code while the old UI/core may still be using the data.

Catalog upgrades validate the package, version, platform, host constraints and
dependencies before changing the selected version. Existing active processes keep
their frozen versions until a complete restart. Old package versions are retained
for rollback; data is not reset or automatically downgraded.
