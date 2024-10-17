import { PubSub, Subscription } from "@google-cloud/pubsub";
import type { Server } from "bun";

const pubsub = new PubSub({
  projectId: process.env.PROJECT_ID,
});

async function createPubSubSubscription(): Promise<string> {
  const topicName = "live-translation";
  const subscriptionName = `stt-sub-${Math.random().toString(36).slice(2, 7)}`;

  console.log(`Creating a new subscription:  ${subscriptionName}`);
  try {
    await pubsub.topic(topicName).createSubscription(subscriptionName, {
      enableMessageOrdering: true,
      enableExactlyOnceDelivery: true,
    });
  } catch (e) {
    console.log("Failed to create a new subscription.", e);
  }

  return subscriptionName;
}

async function startServer(bunSubTopicName: string): Promise<Server> {
  const server = Bun.serve({
    static: {
      "/": new Response(await Bun.file("./public/index.html").bytes(), {
        headers: {
          "Content-Type": "text/html",
        },
      }),
      "/reconnecting-websocket.min.js": new Response(await Bun.file("./public/reconnecting-websocket.min.js").text(), {
        headers: {
          "Content-Type": "text/javascript",
        },
      }),
    },
    async fetch(req, server) {
      const url = new URL(req.url);

      if (url.pathname === "/ws" && server.upgrade(req)) {
        console.log(`upgrade!`);
        return undefined;
      }

      return new Response("Not found", { status: 404 });
    },
    websocket: {
      open(ws) {
        ws.subscribe(bunSubTopicName);
        server.publish(bunSubTopicName, "New client connected");
      },
      message(ws, message) {
        console.log(`Received message from client: ${message}`);
      },
      close(ws) {
        console.log("Client disconnected");
      },
    },
  });

  return server;
}

async function listenForPubSubMessages(server: Server, bunSubTopicName: string, subscription: Subscription) {
  subscription.on("message", async (message) => {
    try {
      const messageContent = JSON.parse(message.data.toString());
      // console.log(`Received message from Pub/Sub: ${JSON.stringify(messageContent)}`);

      server.publish(bunSubTopicName, JSON.stringify(messageContent));

      await message.ackWithResponse();
    } catch (e) {}
  });
}

async function startAll() {
  const subscriptionName = await createPubSubSubscription();
  const subscription = pubsub.subscription(subscriptionName);

  const bunSubTopicName = "stt";
  const server = await startServer(bunSubTopicName);

  await listenForPubSubMessages(server, bunSubTopicName, subscription);

  console.log(`Listening on ${server.hostname}:${server.port}`);
}

startAll();
