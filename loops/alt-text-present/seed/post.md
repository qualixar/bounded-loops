# Behind the Scenes: Building a Bounded Loop

Every bounded loop starts with a broken seed and a real gate. Here's the
architecture diagram we sketched on the whiteboard before writing a line
of code:

![](./whiteboard-diagram.png)

The stub runner replays a recorded cassette against the workspace so the
whole demo runs without any API key:

![A screenshot of the runner logs, showing lap 1 writing output.json and the gate exiting 0](./runner-logs.png)

Once the gate design was settled, we moved straight to the CLI:

![](./cli-screenshot.png)
