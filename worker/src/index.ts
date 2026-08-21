/**
 * Discord interactions endpoint (Cloudflare Worker).
 *
 * Signature verification + PING handshake, busyness button clicks
 * (log:<date>:<key>), and the note/sold-count modal (note:<date> ->
 * note_modal:<date>), all upserting into Postgres.
 */

import { neon } from "@neondatabase/serverless";

export interface Env {
  DISCORD_PUBLIC_KEY: string;
  DISCORD_APPLICATION_ID: string;
  DATABASE_URL: string;
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
  UPDATE_MESSAGE: 7,
  MODAL: 9,
} as const;

const EPHEMERAL = 64; // message flag: visible only to the caller, not persisted

// Sanity range for season_modal's free-text dates ("MM-DD", zero-padded so
// lexical comparison works). Not a DST fix -- the "first weekend of May to
// first weekend of October" formula never crosses a DST boundary by
// construction (docs/PLAN.md) -- this is just insurance against a
// fat-fingered date (e.g. "31.10." instead of "4.10.") slipping through the
// free-text modal with no validation at all.
const SEASON_START_RANGE = ["04-15", "05-15"] as const;
const SEASON_END_RANGE = ["09-15", "10-15"] as const;

// Position must match BUSYNESS in src/log_message.py and the `visited` seed
// order in db/schema.sql (velmi slabe..naval) — the button's key maps to a
// row by name lookup, not a hardcoded id, so reseeding order is the only
// thing that has to stay in sync.
const BUSYNESS: Record<string, { label: string; visitedName: string }> = {
  dead: { label: "Dead", visitedName: "velmi slabe" },
  slow: { label: "Slow", visitedName: "slabe" },
  normal: { label: "Normal", visitedName: "stredni" },
  busy: { label: "Busy", visitedName: "hodne" },
  slammed: { label: "Slammed", visitedName: "naval" },
};

const CZECH_DAYS = ["Neděle", "Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota"];

// Live tracking started 2026-08-01. completeness_sweep.py now looks back to
// season_start rather than the first-ever logged day, so it also surfaces
// pre-tracking weekends recovered from memory — this cutoff is how a click
// gets tagged live vs backfill, since both flow through the same log:
// buttons. ISO YYYY-MM-DD strings compare correctly with plain <.
const LIVE_TRACKING_START = "2026-08-01";

function sourceForDate(isoDate: string): "live" | "backfill" {
  return isoDate < LIVE_TRACKING_START ? "backfill" : "live";
}

function czechDay(isoDate: string): string {
  // Parse as UTC midnight so the weekday can't shift with the Worker's local TZ.
  const day = new Date(`${isoDate}T00:00:00Z`);
  return CZECH_DAYS[day.getUTCDay()];
}

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
      const customId: string = interaction.data.custom_id;
      const [action, isoDate, key] = customId.split(":");

      if (action === "log" && BUSYNESS[key]) {
        await logBusyness(env.DATABASE_URL, isoDate, key);
        return json({
          type: InteractionResponseType.UPDATE_MESSAGE,
          data: { components: confirmDay(interaction.message.components, isoDate, key) },
        });
      }

      if (action === "season_setup") {
        return json({
          type: InteractionResponseType.MODAL,
          data: {
            custom_id: "season_modal",
            title: "Nastavit sezónu",
            components: [
              {
                type: 1,
                components: [{
                  type: 4,
                  custom_id: "season_start",
                  label: "Začátek sezóny (den.měsíc, např. 2.5.)",
                  style: 1,
                  required: true,
                  max_length: 10,
                }],
              },
              {
                type: 1,
                components: [{
                  type: 4,
                  custom_id: "season_end",
                  label: "Konec sezóny (den.měsíc, např. 4.10.)",
                  style: 1,
                  required: true,
                  max_length: 10,
                }],
              },
            ],
          },
        });
      }

      if (action === "note") {
        return json({
          type: InteractionResponseType.MODAL,
          data: {
            custom_id: `note_modal:${isoDate}`,
            title: `Poznámka – ${czechDay(isoDate)} ${isoDate.slice(8, 10)}.${isoDate.slice(5, 7)}`,
            components: [
              {
                type: 1,
                components: [{
                  type: 4, // TEXT_INPUT
                  custom_id: "poznamka",
                  label: "Poznámka (nepovinné)",
                  style: 2, // Paragraph
                  required: false,
                  max_length: 500,
                }],
              },
              {
                type: 1,
                components: [{
                  type: 4,
                  custom_id: "sold_product",
                  label: "Prodáno kusů (nepovinné, celé číslo)",
                  style: 1, // Short
                  required: false,
                  max_length: 6,
                }],
              },
            ],
          },
        });
      }

      return new Response("Unhandled component custom_id", { status: 400 });
    }

    if (interaction.type === InteractionType.MODAL_SUBMIT) {
      const [action, isoDate] = interaction.data.custom_id.split(":");

      if (action === "season_modal") {
        const startRaw = modalValue(interaction.data.components, "season_start").trim();
        const endRaw = modalValue(interaction.data.components, "season_end").trim();
        const year = new Date().getUTCFullYear();
        const start = parseDayMonth(startRaw, year);
        const end = parseDayMonth(endRaw, year);

        if (!start || !end) {
          return json({
            type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
            data: {
              content: `Nerozumím datu "${!start ? startRaw : endRaw}". Použij formát den.měsíc, např. 2.5. Klepni na tlačítko a zkus to znovu.`,
              flags: EPHEMERAL,
            },
          });
        }

        if (!inMonthDayRange(start.iso, SEASON_START_RANGE) || !inMonthDayRange(end.iso, SEASON_END_RANGE)) {
          return json({
            type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
            data: {
              content: `Datum mimo očekávaný rozsah (začátek ${SEASON_START_RANGE[0]}–${SEASON_START_RANGE[1]}, konec ${SEASON_END_RANGE[0]}–${SEASON_END_RANGE[1]}) — zkontroluj překlep. Klepni na tlačítko a zkus to znovu.`,
              flags: EPHEMERAL,
            },
          });
        }

        if (start.iso >= end.iso) {
          return json({
            type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
            data: {
              content: `Začátek sezóny musí být před koncem. Klepni na tlačítko a zkus to znovu.`,
              flags: EPHEMERAL,
            },
          });
        }

        await saveSeasonConfig(env.DATABASE_URL, start.iso, end.iso);
        return json({
          type: InteractionResponseType.UPDATE_MESSAGE,
          data: { components: confirmSeason(interaction.message.components, start.iso, end.iso) },
        });
      }

      if (action === "note_modal") {
        const poznamka = modalValue(interaction.data.components, "poznamka").trim();
        const soldRaw = modalValue(interaction.data.components, "sold_product").trim();

        let soldProduct: number | null = null;
        if (soldRaw !== "") {
          const parsed = Number(soldRaw);
          if (!Number.isInteger(parsed) || parsed < 0) {
            return json({
              type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
              data: {
                content: `"${soldRaw}" není platné celé číslo. Klepni na ADD NOTE a zkus to znovu.`,
                flags: EPHEMERAL,
              },
            });
          }
          soldProduct = parsed;
        }

        const saved = await saveNote(env.DATABASE_URL, isoDate, poznamka, soldProduct);
        if (!saved) {
          return json({
            type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
            data: {
              content: "Nejdřív klepni na busyness tlačítko pro tento den, pak přidej poznámku.",
              flags: EPHEMERAL,
            },
          });
        }
        return json({
          type: InteractionResponseType.UPDATE_MESSAGE,
          data: { components: confirmNote(interaction.message.components, isoDate) },
        });
      }

      return new Response("Unhandled modal custom_id", { status: 400 });
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
 * Upsert into user_input by name-looking-up the visited row instead of a
 * hardcoded id, so reseeding db/schema.sql in a different order can't
 * silently mislabel past clicks.
 *
 * source is set from the date (see LIVE_TRACKING_START) only on first
 * insert. Re-clicking a day only touches visited_id/logged_at (misclick
 * recovery, per the plan) — it never resets poznamka/sold_product/source.
 */
async function logBusyness(databaseUrl: string, isoDate: string, key: string): Promise<void> {
  const sql = neon(databaseUrl);
  const visitedName = BUSYNESS[key].visitedName;
  const rows = await sql`SELECT id_v FROM visited WHERE name_v = ${visitedName}`;
  const visitedId = rows[0].id_v;
  const source = sourceForDate(isoDate);
  await sql`
    INSERT INTO user_input (den, visited_id, source)
    VALUES (${isoDate}, ${visitedId}, ${source})
    ON CONFLICT (den) DO UPDATE SET visited_id = EXCLUDED.visited_id, logged_at = now()
  `;
}

/**
 * Parses "den.měsíc" (e.g. "2.5." or "2.5") against the given year. Returns
 * null on anything that doesn't cleanly parse to a real calendar date — the
 * caller re-prompts rather than guessing.
 */
function parseDayMonth(input: string, year: number): { iso: string } | null {
  const match = input.match(/^(\d{1,2})\.(\d{1,2})\.?$/);
  if (!match) return null;
  const day = Number(match[1]);
  const month = Number(match[2]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const date = new Date(Date.UTC(year, month - 1, day));
  // Catches e.g. 31.4. (April has 30 days) — Date normalizes overflow instead
  // of erroring, so compare back against what was asked for.
  if (date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  const iso = date.toISOString().slice(0, 10);
  return { iso };
}

/** True when iso's "MM-DD" falls within [range[0], range[1]] inclusive. */
function inMonthDayRange(iso: string, range: readonly [string, string]): boolean {
  const monthDay = iso.slice(5, 10);
  return monthDay >= range[0] && monthDay <= range[1];
}

/**
 * Upserts the singleton season_config row (id=1) — always the current
 * season's window, overwriting whatever was there before.
 */
async function saveSeasonConfig(databaseUrl: string, startIso: string, endIso: string): Promise<void> {
  const sql = neon(databaseUrl);
  await sql`
    INSERT INTO season_config (id, season_start, season_end)
    VALUES (1, ${startIso}, ${endIso})
    ON CONFLICT (id) DO UPDATE SET
      season_start = EXCLUDED.season_start,
      season_end = EXCLUDED.season_end,
      updated_at = now()
  `;
}

/** Replaces the "set season" button row with a confirmation text block. */
function confirmSeason(components: any[], startIso: string, endIso: string): any[] {
  const fmt = (iso: string) => `${Number(iso.slice(8, 10))}.${Number(iso.slice(5, 7))}.${iso.slice(0, 4)}`;
  return components.map((component) => {
    const isSeasonRow =
      component.type === 1 &&
      component.components?.some((c: any) => c.custom_id === "season_setup");
    if (!isSeasonRow) return component;
    return { type: 10, content: `✅ Sezóna nastavena: ${fmt(startIso)} – ${fmt(endIso)}` };
  });
}

function modalValue(actionRows: any[], customId: string): string {
  for (const row of actionRows) {
    for (const c of row.components) {
      if (c.custom_id === customId) return c.value ?? "";
    }
  }
  return "";
}

/**
 * Only UPDATEs — never INSERTs — because user_input.visited_id is NOT NULL,
 * so a note can't be the first thing logged for a day (matches the plan's
 * flow: tap a busyness button first, ADD NOTE is the optional follow-up).
 * Blank fields leave the existing column value alone, so filling in just
 * sold_product doesn't wipe out a poznamka from an earlier note edit.
 * Returns false when no row exists yet for that date.
 */
async function saveNote(
  databaseUrl: string,
  isoDate: string,
  poznamka: string,
  soldProduct: number | null,
): Promise<boolean> {
  const sql = neon(databaseUrl);
  const rows = await sql`
    UPDATE user_input
    SET poznamka = COALESCE(NULLIF(${poznamka}, ''), poznamka),
        sold_product = COALESCE(${soldProduct}, sold_product)
    WHERE den = ${isoDate}
    RETURNING id_up
  `;
  return rows.length > 0;
}

/**
 * Replace the clicked day's 5-button busyness row with a ✅ confirmation
 * text block, leaving its header and "ADD NOTE" row untouched. Matches by
 * scanning each action row's buttons for a "log:<isoDate>:" custom_id
 * rather than by array position, since the message layout (day blocks +
 * a divider) isn't something this handler should have to know by heart.
 */
function confirmDay(components: any[], isoDate: string, key: string): any[] {
  const prefix = `log:${isoDate}:`;
  return components.map((component) => {
    const isTargetRow =
      component.type === 1 &&
      component.components?.some((c: any) => c.custom_id?.startsWith(prefix));
    if (!isTargetRow) return component;
    return {
      type: 10,
      content: `✅ ${czechDay(isoDate)} ${isoDate.slice(8, 10)}.${isoDate.slice(5, 7)} — ${BUSYNESS[key].label}`,
    };
  });
}

/**
 * Insert a "poznámka přidána" marker right after the day's ADD NOTE row
 * (which stays untouched/clickable, so editing the note again still works).
 * Drops any marker already sitting there from a previous note edit first,
 * so re-submitting the modal doesn't stack up duplicate markers.
 */
function confirmNote(components: any[], isoDate: string): any[] {
  const noteId = `note:${isoDate}`;
  const marker = { type: 10, content: "*poznámka přidána*" };
  const result: any[] = [];
  components.forEach((component, i) => {
    const prevWasNoteRow =
      i > 0 &&
      components[i - 1].type === 1 &&
      components[i - 1].components?.some((c: any) => c.custom_id === noteId);
    const isStaleMarker = component.type === 10 && component.content === marker.content && prevWasNoteRow;
    if (isStaleMarker) return;

    result.push(component);
    const isNoteRow = component.type === 1 && component.components?.some((c: any) => c.custom_id === noteId);
    if (isNoteRow) result.push(marker);
  });
  return result;
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
