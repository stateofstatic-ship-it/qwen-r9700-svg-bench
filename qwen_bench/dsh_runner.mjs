/** Genuine DSH Agent driver: one process, one session, four exact user turns. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';
import { appendFileSync, statSync } from 'node:fs';
import { createInterface } from 'node:readline';

const requireDsh = createRequire(process.env.SVG_BENCH_DSH_PACKAGE);
const native = async name => import(pathToFileURL(requireDsh.resolve(name)).href);
const { installModelSelection } = await native('@deepseek-ai/dsh-agent');
const { createUserMessage } = await native('@deepseek-ai/dsh-llm');
const { SessionId, SessionSeq } = await native('@deepseek-ai/dsh-session');
const MAX_LINE = 1024 * 1024;
const MAX_LOG = 64 * 1024 * 1024;
export const name = 'svg-benchmark-runner';
export const inject = ['agentDefaultModel', 'agents', 'sessions'];

function send(event) {
  const line = JSON.stringify({__dsh_bench__: 1, ...event}) + '\n';
  if (Buffer.byteLength(line) > MAX_LINE) throw new Error('Runner event exceeds 1 MiB');
  process.stdout.write(line);
}
const digest = value => createHash('sha256').update(value).digest('hex');

export function apply(ctx) {
  const exit = ctx.get('appExit');
  if (!exit) throw new Error('DSH launcher appExit is required');
  run(ctx, exit).catch(error => {
    send({type: 'error', checkpoint: null, message: String(error.message).slice(0, 2048)});
    exit(1);
  });
}

async function run(ctx, exit) {
  await ctx.get('loader')?.await();
  const agents = ctx.get('agents');
  const sessions = ctx.get('sessions');
  const defaultModel = ctx.get('agentDefaultModel');
  // Loader/service replacement can dispose this invocation during initial boot.
  // Like stock dsh-headless, let its replacement invocation own the agent.
  if (!agents || !sessions || !defaultModel) return;
  const selection = defaultModel.currentSelection();
  const id = SessionId('session-' + randomUUID());
  const rawPath = process.env.SVG_BENCH_DSH_RAW;
  let observerError;
  let checkpoint = null;
  let rawBytes = 0;
  const dispose = ctx.on('session/event', (session, event) => {
    if (session.id !== id || observerError) return;
    try {
      const raw = JSON.stringify({session_id: id, checkpoint, event}) + '\n';
      rawBytes += Buffer.byteLength(raw);
      if (rawBytes > MAX_LOG) throw new Error('Native durable event capture exceeds 64 MiB');
      appendFileSync(rawPath, raw, {mode: 0o600});
      const common = {checkpoint, session_id: id, native_seq: event.seq, native_type: event.type, raw_trace: rawPath};
      if (event.type === 'tool/call') {
        send({...common, type: 'tool.started', tool_call_id: event.data.callId,
          name: event.data.name, arguments: event.data.arguments.slice(0, 16384),
          arguments_sha256: digest(event.data.arguments), truncated: event.data.arguments.length > 16384});
      } else if (event.type === 'tool/result') {
        const encoded = JSON.stringify(event.data);
        send({...common, type: 'tool.completed', native: encoded.length <= 16384 ? event.data : undefined,
          native_sha256: digest(encoded), native_bytes: Buffer.byteLength(encoded), truncated: encoded.length > 16384});
      } else if (event.type === 'assistant/message') {
        const text = event.data.message.content.filter(b => b.type === 'text').map(b => b.text).join('');
        if (text) send({...common, type: 'assistant.message', text: text.slice(0, 16384),
          content: text.slice(0, 16384), text_sha256: digest(text), truncated: text.length > 16384});
      }
    } catch (error) { observerError = error; }
  });
  const {agent} = await agents.create({
    sessionId: id, meta: {cwd: process.cwd()},
    agentOptions: {provider: selection.provider, model: selection.model,
      reasoningEffort: selection.reasoningEffort, maxTokens: Number(process.env.SVG_BENCH_DSH_MAX_TOKENS)},
    setup(agentCtx) { installModelSelection(agentCtx, {current: selection, assembled: undefined}); },
  });
  await agent.whenIdle();
  await sessions.flush(agent.session);
  if (observerError) throw observerError;
  send({type: 'native.ready', checkpoint: null, session_id: agent.session.id});
  let turns = 0;
  const lines = createInterface({input: process.stdin, crlfDelay: Infinity, terminal: false});
  try {
    for await (const line of lines) {
      if (Buffer.byteLength(line) > MAX_LINE) throw new Error('Runner request exceeds 1 MiB');
      const request = JSON.parse(line);
      if (request.type === 'shutdown') {
        await agent.whenIdle();
        await sessions.flush(agent.session);
        if (observerError) throw observerError;
        send({type: 'native.closed', checkpoint: null, session_id: id, turns});
        dispose();
        exit(0);
        return;
      }
      if (request.type !== 'turn' || turns >= 4 || request.checkpoint !== 'C' + turns || request.session_id !== id)
        throw new Error('Invalid native turn order or session');
      checkpoint = request.checkpoint;
      const bytes = Buffer.from(request.prompt_base64, 'base64');
      if (digest(bytes) !== request.prompt_sha256) throw new Error('Native prompt digest mismatch');
      const text = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
      const firstSeq = agent.session.seq;
      send({type: 'turn.started', checkpoint, session_id: id, prompt_sha256: digest(bytes)});
      agent.followup(createUserMessage({content: [{type: 'text', text}], source: {kind: 'user'}}));
      await agent.whenIdle();
      await sessions.flush(agent.session);
      if (observerError) throw observerError;
      const events = [];
      for (let seq = firstSeq; seq < agent.session.seq; seq++) {
        const event = agent.session.eventAt(SessionSeq(seq));
        if (!event) throw new Error('Missing native session event');
        events.push(event);
      }
      const starts = events.filter(e => e.type === 'turn/start');
      const ends = events.filter(e => e.type === 'turn/end');
      const users = events.filter(e => e.type === 'user/message' && e.data.source?.kind === 'user');
      if (starts.length !== 1 || ends.length !== 1 || ends[0].data.reason.kind !== 'completed')
        throw new Error('Native turn did not complete exactly once; inspect private durable trace');
      if (users.length !== 1 || users[0].data.content.length !== 1 || users[0].data.content[0].text !== text)
        throw new Error('Native exact user prompt evidence missing');
      const usage = events.filter(e => e.type === 'assistant/message').map(e => ({
        native_seq: e.seq, turn: e.data.turn, step: e.data.step, usage: e.data.usage ?? null,
      }));
      if (statSync(rawPath).size > MAX_LOG) throw new Error('Native raw event log exceeded limit');
      turns++;
      send({type: 'turn.completed', checkpoint, session_id: id, status: 'completed',
        native_turn: ends[0].data.turn, native_terminal: ends[0], first_seq: firstSeq,
        last_seq: agent.session.seq - 1, prompt_sha256: digest(bytes),
        usage: {scope: 'native per-step provider usage; not summed', steps: usage},
        terminal_source: 'native_turn_end_completed_after_whenIdle_and_session_flush', raw_trace: rawPath});
      checkpoint = null;
    }
    throw new Error('Parent input closed before shutdown');
  } finally {
    lines.close();
    dispose();
  }
}
