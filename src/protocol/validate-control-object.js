import crypto from 'node:crypto';
import { CLASSIFICATIONS, CIRCUIT_STATES, MESSAGE_STATUSES, MISSION_STATES, PROTOCOL_VERSION, ROLES } from './constants.js';

const isNonEmptyString = (value) => typeof value === 'string' && value.length > 0;
const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;
const isSha256 = (value) => typeof value === 'string' && /^[0-9a-fA-F]{64}$/.test(value);

export function validateControlObject(bundle) {
  const defects = [];
  const fail = (code, detail) => defects.push({ code, detail });
  try {
    if (!bundle || typeof bundle !== 'object') throw new Error('state bundle must be an object');
    const { mission, activeArchitect, messages, promptIntegrity, journal, intents = [] } = bundle;
    if (!mission || typeof mission !== 'object') fail('INVALID_MISSION', 'mission is required');
    if (mission?.protocolVersion !== PROTOCOL_VERSION) fail('INVALID_MISSION', 'unsupported or missing protocolVersion');
    for (const field of ['missionId', 'currentMilestone', 'currentRole']) if (!isNonEmptyString(mission?.[field])) fail('INVALID_MISSION', `${field} must be a non-empty string`);
    if (!MISSION_STATES.includes(mission?.missionState)) fail('INVALID_MISSION_STATE', mission?.missionState);
    if (!isPositiveInteger(mission?.activeArchitectGeneration)) fail('INVALID_ARCHITECT_GENERATION', 'must be a positive integer');
    if (!CIRCUIT_STATES.includes(mission?.circuitState)) fail('INVALID_CIRCUIT_STATE', mission?.circuitState);
    if (!ROLES.includes(mission?.currentRole)) fail('INVALID_ROLE', mission?.currentRole);
    if (!activeArchitect || typeof activeArchitect !== 'object') fail('INVALID_AUTHORITY', 'activeArchitect is required');
    if (activeArchitect?.missionId !== mission?.missionId || activeArchitect?.generation !== mission?.activeArchitectGeneration || activeArchitect?.state !== 'ACTIVE' || !isPositiveInteger(activeArchitect?.generation)) fail('STALE_ARCHITECT_GENERATION', 'active authority does not match mission');
    if (!Array.isArray(messages)) fail('INVALID_MESSAGES', 'messages must be an array');
    const ids = new Set();
    for (const message of messages ?? []) {
      if (!message || typeof message !== 'object') { fail('INVALID_MESSAGE', 'message must be an object'); continue; }
      for (const field of ['messageId', 'missionId', 'milestone', 'senderRole', 'recipientRole', 'status']) if (!isNonEmptyString(message[field])) fail('INVALID_MESSAGE_FIELD', field);
      if (!Object.hasOwn(message, 'parentMessageId')) fail('INVALID_PARENT_MESSAGE', 'parentMessageId field is required');
      if (isNonEmptyString(message.messageId)) {
        if (ids.has(message.messageId)) fail('DUPLICATE_MESSAGE_ID', message.messageId);
        ids.add(message.messageId);
      }
      if (message.missionId !== mission?.missionId) fail('MESSAGE_MISSION_MISMATCH', message.messageId);
      if (!ROLES.includes(message.senderRole)) fail('INVALID_SENDER_ROLE', message.messageId);
      if (!ROLES.includes(message.recipientRole)) fail('INVALID_RECIPIENT_ROLE', message.messageId);
      if (!MESSAGE_STATUSES.includes(message.status)) fail('INVALID_MESSAGE_STATUS', message.status);
      if (message.parentMessageId !== null) {
        if (!isNonEmptyString(message.parentMessageId) || !ids.has(message.parentMessageId)) fail('INVALID_PARENT_MESSAGE', message.messageId);
        if (message.parentMessageId === message.messageId) fail('INVALID_PARENT_MESSAGE', message.messageId);
      }
      if (message.status === 'OPEN') {
        if (message.milestone !== mission?.currentMilestone) fail('OPEN_MILESTONE_MISMATCH', message.messageId);
        if (message.recipientRole !== mission?.currentRole) fail('OPEN_RECIPIENT_MISMATCH', message.messageId);
        if (message.senderRole !== 'Architect') fail('OPEN_SENDER_INVALID', message.messageId);
      }
    }
    if (!promptIntegrity || typeof promptIntegrity !== 'object' || typeof promptIntegrity.canonicalPrompt !== 'string' || !isSha256(promptIntegrity.expectedSha256) || !isSha256(promptIntegrity.observedSha256)) fail('PROMPT_HASH_MISMATCH', 'canonical prompt and valid SHA-256 fields are required');
    else {
      const computedSha256 = sha256(promptIntegrity.canonicalPrompt);
      if (computedSha256 !== promptIntegrity.expectedSha256 || promptIntegrity.expectedSha256 !== promptIntegrity.observedSha256) fail('PROMPT_HASH_MISMATCH', 'computed, expected, and observed hashes must agree');
    }
    if (!Array.isArray(journal)) fail('INVALID_JOURNAL', 'journal must be an array');
    let previous = null;
    for (const entry of journal ?? []) {
      if (!entry || typeof entry !== 'object' || !isNonEmptyString(entry.eventId)) { fail('INVALID_JOURNAL', 'eventId is required'); continue; }
      if (entry.previousEventId !== previous) fail('JOURNAL_CHAIN_INVALID', entry.eventId);
      if (entry.bootId === undefined || entry.missionId !== mission?.missionId) fail('JOURNAL_METADATA_INVALID', entry.eventId);
      previous = entry.eventId;
    }
    const intentList = Array.isArray(intents) ? intents : [];
    const intentIds = new Set(intentList.filter((x) => x?.type === 'INTENT').map((x) => x.messageId ?? x.intentId));
    const resultIds = new Set(intentList.filter((x) => x?.type === 'RESULT').map((x) => x.messageId ?? x.intentId));
    const unresolved = [...intentIds].filter((id) => !resultIds.has(id));
    if (defects.length) return { classification: CLASSIFICATIONS.FAIL_CLOSED, retryAuthorized: false, defects };
    if (unresolved.length) return { classification: CLASSIFICATIONS.RECONCILIATION_REQUIRED, retryAuthorized: false, defects: [{ code: 'INTENT_WITHOUT_RESULT', detail: unresolved }] };
  } catch (error) {
    fail('INVALID_BUNDLE', error.message);
  }
  return defects.length ? { classification: CLASSIFICATIONS.FAIL_CLOSED, retryAuthorized: false, defects } : { classification: CLASSIFICATIONS.HEALTHY, retryAuthorized: false, defects: [] };
}

export function sha256(text) { return crypto.createHash('sha256').update(text, 'utf8').digest('hex'); }
