/**
 * Discord interactions endpoint (Cloudflare Worker).
 *
 * Phase 2 step 1: signature verification + the PING handshake Discord
 * requires before it will let you save an Interactions Endpoint URL.
 * Button clicks and modal submits are handled as stubs for now — wiring
 * them to Postgres is the next step, once this deploys and Discord's
 * portal accepts the URL.
 */

export interface Env {
  DISCORD_PUBLIC_KEY: string;
  DISCORD_APPLICATION_ID: string;
}

const InteractionType = {
  PING: 1,
  APPLICATION_COMMAND: 2,
  MESSAGE_COMPONENT: 3,
  MODAL_SUBMIT: 5,
} as const;

const InteractionResponseType = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
} as const;

const EPHEMERAL = 64; // message flag: visible only to the caller, not persisted

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("Expected POST", { status: 405 });
    }

    const { isValid, body } = await verifyDiscordRequest(request, env.DISCORD_PUBLIC_KEY);
    if (!isValid) {
      return new Response("Bad request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    if (interaction.type === InteractionType.PING) {
      return json({ type: InteractionResponseType.PONG });
    }

    if (interaction.type === InteractionType.MESSAGE_COMPONENT) {
      // TODO: busyness button click -> upsert into user_input (next step)
      return json({
        type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        data: { content: "Button handling isn't wired up yet.", flags: EPHEMERAL },
      });
    }

    if (interaction.type === InteractionType.MODAL_SUBMIT) {
      // TODO: note + sold-product modal -> upsert into user_input (next step)
      return json({
        type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        data: { content: "Modal handling isn't wired up yet.", flags: EPHEMERAL },
      });
    }

    return new Response("Unhandled interaction type", { status: 400 });
  },
};

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Ed25519 signature check Discord requires on every interaction POST.
 * Uses the Workers runtime's native WebCrypto Ed25519 support directly —
 * no external verification library needed.
 */
async function verifyDiscordRequest(
  request: Request,
  publicKeyHex: string | undefined,
): Promise<{ isValid: boolean; body: string }> {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  const body = await request.text();

  if (!signature || !timestamp || !publicKeyHex) {
    return { isValid: false, body };
  }

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKeyHex),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    const isValid = await crypto.subtle.verify(
      "Ed25519",
      key,
      hexToBytes(signature),
      new TextEncoder().encode(timestamp + body),
    );
    return { isValid, body };
  } catch {
    return { isValid: false, body };
  }
}

function hexToBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}
