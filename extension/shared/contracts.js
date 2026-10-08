/**
 * Runtime-free API contracts. These JSDoc types are consumed by editors and
 * TypeScript's checkJs mode while the same files run directly in Chrome.
 *
 * @typedef {'online'|'offline'|'revoked'|'unauthorized'|'permission_denied'|'version_mismatch'|'unpaired'} ConnectionState
 * @typedef {{id:string,name:string,status?:string}} Campaign
 * @typedef {{kind:'selection'|'page',url:string,title:string,selection?:string,description?:string,capturedAt:string}} CapturePayload
 * @typedef {{previewId:string,capture:CapturePayload,warnings:Array<{kind:'existing_contact'|'do_not_contact',message:string,blocking:boolean}>,suggestedCampaignId?:string}} CapturePreview
 * @typedef {{notifications:number,reviewQueue:number,followupsDue:number,campaigns:Campaign[]}} HermesSummary
 * @typedef {{baseUrl:string,deviceToken:string|null,mockMode:boolean,backendVersion?:string,apiVersion?:string}} ConnectionSettings
 */

export const API_VERSION = "2026-10-01";
export const EXTENSION_VERSION = "0.1.0";
export const MAX_SELECTION_CHARS = 8_000;
export const MAX_DESCRIPTION_CHARS = 500;

export const STORAGE_KEYS = Object.freeze({
  settings: "connectionSettings",
  pendingCapture: "pendingCapture",
  mockState: "mockState"
});

export const MESSAGE = Object.freeze({
  getState: "GET_STATE",
  pair: "PAIR",
  revoke: "REVOKE",
  api: "API_CALL",
  capture: "CAPTURE_ACTIVE_TAB",
  openOptions: "OPEN_OPTIONS",
  refreshBadge: "REFRESH_BADGE"
});
