# Pipelines
Here you can create your own graph topologies that the agent will follow.
There is only one execution flow. Parallel calls are implemented through a dedicated node.
After certain nodes, the flow can be looped back.

Cards have three main types of connections:
Exec - determines the order of operations in a single execution flow.
Prompt - passes text instructions to the model. A Prompt input can accept multiple connections.
Arg - passes values and structured data between cards. An Arg input cannot accept more than one connection.

Prompt and Arg connections do not create independent execution flows. They are used to pass data that the corresponding cards will need. Cyclic dependencies between these connections are prohibited.
You do not have to connect them. If a node's Prompt and Arg inputs remain empty, the model simply won't receive an additional prompt and will generate the arguments itself.

Some cards perform operations on their own, without calling the model. Examples include data transformation, executing Python code, and sending notifications.
Other cards can involve the main model, subagents, or the user in making decisions, generating arguments, and selecting the next route.

This allows a pipeline to combine a predefined sequence of operations with decisions made during execution.

For each supported executable card, you can configure error handling: stop execution, retry an operation, skip it, or route the flow into a separate error-handling branch.

There are also termination points. Finish subgraph ends the current pipeline and returns its result, while Shut down graph terminates the entire agent run.
Execution state is also saved, allowing interrupted runs to be resumed when an appropriate recovery point is available.

Before execution, the pipeline is checked for connection validity, tool availability, cyclic dependencies, and other constraints. You can also run this check manually from the editor.

Pipelines can also be published through Toshy MCP, making them callable by external applications and AI agents.

## Cards
1. Prompt - A card containing text that will be passed to the model as an additional request before executing a graph step.
	
2. Router - Allows branches of any size and redirects execution to specific parts of the pipeline. The mechanism for selecting a route is configured in the card's settings. You can create loops by connecting the router backward, or skip portions of a branch by connecting it further ahead.
	
3. Loop - Redirects the execution flow until the condition for continuing the branch is met. You can set the maximum number of iterations and configure what is passed forward: only the result of the successful iteration or the results of all iterations. The mechanism for determining whether an iteration succeeds is configured in the card's settings. Like the router, it allows the flow to be routed backward or forward.
	
4. Parallel tools - Instructs the model to call multiple tools at once. You can configure either parallel execution or sequential calls. In the latter case, there is still only one reasoning block before all the calls; the tools themselves are simply called sequentially. Their order also matters in this mode. Only one subagent call node can be included in Parallel tools, but it can be used to continue a chain of subagent calls.
	
5. Human-in-the-loop - Pauses the graph and sends a confirmation question to the user's chat. Allows requesting argument values from the user.
	
6. Shut down graph - Completely stops the ENTIRE generation.
	
7. Finish subgraph - Ends the current pipeline while allowing generation to continue.
	
8. Notification - Sends a notification to the user's chat and automatically continues pipeline execution.
	
9. Nested Pipeline - Starts another pipeline in the same module and waits for it to finish.
	
10. Python Code - A versatile node in which you can do just about anything with arguments. More details below.
	
11. Data transformation - Works with different data formats; for example, extracting a date from JSON. More details below.

13. Autonomous agent decision - The model can call whatever it wants, or nothing at all.
	
14. Argument nodes contain typed values and are used to specify node arguments manually.
	
As for module tool cards, everything is fairly standard. The only one worth mentioning separately is the subagent call node:
It has two outputs. The first is disabled when the second is connected, and the second can only be connected to another subagent call node. The second output is needed to build chains of subagent calls so that subagents can pass context to one another without going through the main agent.

### Python Code

The node executes a Python script and passes results only through arguments. Nothing is passed through the Exec flow.

In the settings, add input and output arguments. Specify a name and type for each. Names must be valid Python variable names and unique within their respective lists.

Connect data to the input Arg ports. In the code, they are available as ordinary variables with the specified names. The system can ask the model to supply missing input values.

Write the script in the code field. For every declared output, assign a value to a variable with exactly the same name. You do not need to wrap the script in a function; top-level return is not used.

Output values must be JSON-serializable and match their declared types. A missing value for even one output is an error. print() is recorded in the execution report but does not replace output arguments.

The script runs in a separate process with the project folder as its working directory. The model does not participate in computations while it executes. The process has access to the user's files and environment: this is not a secure sandbox.

### Data transformation

The node processes an input value through a sequence of configured operations and passes results only through arguments. Nothing is passed through the Exec flow.

In the settings, add operations with the "+" button. Choose an action for each and fill in its parameters.
Arrange the operations in the desired order using the move buttons. Each operation receives the result of the previous one.

Available operations include JSON processing, extracting and renaming fields, filtering and sorting arrays, removing duplicates, processing text and regular expressions, and converting dates, time zones, and types.

Paths to nested values can be specified with dot notation or JSON Pointer syntax. Array elements are addressed by zero-based indices. An empty path refers to the entire current value.

If the input contains JSON as a string, enable automatic JSON parsing before field or array operations, or add a separate JSON parsing operation.
