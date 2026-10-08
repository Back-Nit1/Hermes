import { API_VERSION } from "../shared/contracts.js";
import { normalizeCapture } from "../shared/capture.js";

const initialState = () => ({
  previews: {},
  captures: [],
  campaigns: [
    { id: "cmp_neuro", name: "Computational neurodevelopment", status: "active" },
    { id: "cmp_climate", name: "Climate founders", status: "active" },
    { id: "cmp_speakers", name: "Rutgers speaker series", status: "active" }
  ],
  notifications: [
    { id: 1, title: "Draft ready for review", detail: "Dr. Maya Chen · Computational neurodevelopment", kind: "review" },
    { id: 2, title: "Follow-up due today", detail: "Avery Singh · Climate founders", kind: "followup" }
  ],
  reviewQueue: [
    { id: "c_maya", campaignId: "cmp_neuro", name: "Dr. Maya Chen", status: "Needs approval" },
    { id: "c_avery", campaignId: "cmp_climate", name: "Avery Singh", status: "Evidence review" }
  ],
  followups: [
    { id: "f_1", campaignId: "cmp_climate", name: "Avery Singh", due: "Today", status: "Needs review" },
    { id: "f_2", campaignId: "cmp_speakers", name: "Jordan Ellis", due: "Tomorrow", status: "Scheduled" }
  ]
});

export class MockTransport {
  constructor(storage) { this.storage = storage; }

  async state() {
    const data = await this.storage.get("mockState");
    return data.mockState || initialState();
  }

  async save(state) { await this.storage.set({ mockState: state }); }

  async pair(code) {
    if (!/^HERMES-[A-Z0-9]{4}$/i.test(String(code).trim())) throw Object.assign(new Error("Use a demo code like HERMES-DEMO."), { state: "unauthorized" });
    return { deviceToken: "mock-device-token", backendVersion: "mock-0.1", apiVersion: API_VERSION };
  }

  async revoke() { return { revoked: true }; }

  async summary() {
    const state = await this.state();
    return { apiVersion: API_VERSION, notifications: state.notifications.length, reviewQueue: state.reviewQueue.length, followupsDue: state.followups.filter((x) => x.due === "Today").length, campaigns: state.campaigns, notificationItems: state.notifications, reviewItems: state.reviewQueue, followupItems: state.followups };
  }

  async campaigns() { return { apiVersion: API_VERSION, campaigns: (await this.state()).campaigns }; }

  async preview(rawCapture) {
    const state = await this.state();
    const capture = normalizeCapture(rawCapture);
    const text = `${capture.title} ${capture.selection || ""}`.toLowerCase();
    const warnings = [];
    if (text.includes("maya")) warnings.push({ kind: "existing_contact", message: "This person already appears in Computational neurodevelopment.", blocking: false });
    if (text.includes("do not contact") || text.includes("dnc")) warnings.push({ kind: "do_not_contact", message: "This contact is on the do-not-contact list. Commit is blocked.", blocking: true });
    const previewId = `preview_${Date.now().toString(36)}`;
    state.previews[previewId] = { previewId, capture, warnings, suggestedCampaignId: state.campaigns[0].id };
    await this.save(state);
    return { apiVersion: API_VERSION, ...state.previews[previewId] };
  }

  async commit(previewId, campaignId) {
    const state = await this.state();
    const preview = state.previews[previewId];
    if (!preview) throw Object.assign(new Error("Preview expired. Capture the page again."), { state: "offline" });
    if (!state.campaigns.some((x) => x.id === campaignId)) throw Object.assign(new Error("Choose a valid campaign."), { state: "permission_denied" });
    if (preview.warnings.some((x) => x.blocking)) throw Object.assign(new Error("This contact is marked do not contact."), { state: "permission_denied" });
    state.captures.push({ id: `capture_${Date.now().toString(36)}`, campaignId, capture: preview.capture });
    delete state.previews[previewId];
    await this.save(state);
    return { apiVersion: API_VERSION, committed: true, campaignId, captureId: state.captures.at(-1).id };
  }
}
