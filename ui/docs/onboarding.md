Смена языка: Нижняя панель -> Startup -> Поиск по настройкам -> Personalization

# Instant start
Bottom panel -> Startup -> Startup button -> Below, in the settings "API" -> Create

# Getting started with Toshy Studio

Hi! Here's a quick guide to getting started with this application.
There are just three main elements that make up almost the entire interface:
1. **Top / Bottom panels** - Contain workspace tabs and other controls.
2. **Workspaces** - You're reading this in the "File" workspace right now. These are the areas where you actually work on your projects or the agent.
3. **Widgets** - These are tabs in the side panels, providing features you may need regardless of the current workspace.
If you close this tab, you can reopen it by clicking (i) in the top panel and selecting "Getting started with Toshy".

To start the agent core, open the "Startup" workspace in the bottom panel. On subsequent launches of the application, "Startup" will open automatically. It contains the settings; for now, we're only interested in "API" and "Personalization". Configure whatever you need there. Then click the round "Startup" button above the settings panel, at the center of the orbital rings. The button is surrounded by segments — these are module switches. The inner ring contains the built-in system modules. The outer ring will contain your custom modules with the documentation, MCP servers, pipelines, and Skills you need. More on that later.

Now read this paragraph and switch to the Chat workspace — its tab is in the top panel. Try typing something and sending it to the model. Notice that a project is automatically created in the "Sessions" widget, with your new session inside it. A project is a folder for sessions, and sessions inherit its settings, including the working folder, available modules, system prompt, and so on. You can configure them in the "Projects" workspace in the top panel.

By the way, Chat has a lot of features. Hover over a message and a bunch of buttons will appear. Hover over each one, and after a moment a tooltip will tell you what it does. You'll find these tooltips throughout the application. If they become annoying over time, you can disable them in the settings. There are also plenty of features in the message input panel. Let's go through them — this is especially important:
- The plus button simply opens the file system so you can send any file to the model.
- The tag icon opens the tags panel. You can enable or disable tags, add new ones, and remove them. Tags are simply short instructions for the model that are attached to your messages as part of the request. To the model, they look something like "For this message only: *Tag*".
- Under the wrench icon you'll find:
	- "Clean request" sends a request without passing context or system prompts. This is useful, for example, when running a model that doesn't carry on a conversation but simply processes any input data.
	- "Allow full web page reading for this request" makes web search results load directly into the model's context rather than into memory, which it would normally access.
	- The "Skills" and "Pipelines" options make the model use the items you specify, provided they exist in custom modules.
- The information icon lets you view the fully assembled system prompt (only slightly reformatted for readability), the compressed context as the model sees it, and a button to open the Memory manager — a panel where you can view and delete entries in session memory and the agent's global memory.
- The next two buttons control reasoning settings and the selected model.
- On the right there's also the send button, with the button for interrupting the model's thinking to its left; while generating, a stop-generation button appears even further left.
Other things worth knowing:
- You can send additional requests during generation, and the model will take them into account within the same generation.
- You can select messages in batches by holding the left mouse button and dragging along the left side of them.
- When editing your own request, you can immediately regenerate the model's response.

Moving on. In the bottom-left corner, there are two widget tabs: "Project Folder" and "Materials". What's the difference? The agent works with the Project Folder, and any non-.md files you create in the "File" workspace will also be created in the Project Folder. Materials, on the other hand, is storage for .md files from the text editor, .canvas files from Board, and .tpaint files from Sketch. This keeps your notes from cluttering the project, and vice versa. If you want, you can set the Materials widget to use the same folder as the working directory. Meanwhile, the workspaces show all the tabs you have open at once, regardless of which project or working folder is currently selected.

You may have noticed the "Tasks" widget in the top-right corner. You can also see tasks in the session settings in the "Projects" workspace. While working, the model creates tasks on its own or when explicitly asked, and those tasks immediately receive the "In progress" status. You can also create tasks yourself. The model cannot close tasks on its own; that only happens when you explicitly request it with the "Update statuses" button — so the model can't decide for you whether you've finished working on something. This system lets you quickly see what was done and what remains unfinished in a particular session.

I mentioned custom modules earlier. You create them in the "Module" workspace in the bottom panel. There you can download MCP servers; turn files or web pages into databases that the agent can consult; add pipelines, which I'll cover next; and add Skills. Generally, Toshy puts the emphasis on pipelines. Skills are supported, but mainly for compatibility.

And finally, the best part: the "Pipeline" workspace. This is where you build the graph topologies Toshy will follow. It's a graphical editor where you simply add cards and connect them. I tried to make it as simple as possible by packaging more complex features into individual nodes or their settings, so you don't have to untangle a puzzle of hundreds of nodes as you sometimes do in other agent systems. For detailed information about the editor itself and the available nodes, click the book icon on the floating vertical toolbar in the top-right corner (while you're in the Pipeline workspace).

That's about it for onboarding. Feel free to explore the rest yourself. There isn't proper documentation yet =)
One more thing: under the (i) button in the top panel, you can open the plugin manager. There should be a plugin available there for connecting your ChatGPT subscription.
