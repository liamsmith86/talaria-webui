import { apiURL } from "./profile-context.js";
import { msg } from "./i18n.js";

export function errorMessage(value, fallback = msg("Something went wrong. Please try again.")) {
  const text = typeof value === "string" ? value : value?.message;
  return typeof text === "string" && text.trim() ? text.slice(0, 4096) : fallback;
}

let transport;
export const setTransport = (value) => { transport = value; };
export const request = (path, options) => transport
  ? transport.request(path, options) : fetch(apiURL(path), options);
export const eventSource = (path) => transport
  ? transport.events(path) : new EventSource(apiURL(path));

let csrf = "";
export const setCSRF = (value) => {
  csrf = value;
};
export class RequestError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
export async function api(path, options = {}) {
  let response;
  try {
    response = await request(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Talaria-Request": "1",
        "X-CSRF-Token": csrf,
        ...options.headers,
      },
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new RequestError(
      msg("Connection interrupted. Your work is still with Hermes."),
      0,
      "offline",
    );
  }
  let data;
  try {
    data = response.status === 204 ? {} : await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw error;
    // A request may reach Hermes even when its response body is lost. In
    // particular, never acknowledge a submission by substituting an empty body.
    if (response.ok)
      throw new RequestError(
        msg("The server response could not be read."),
        0,
        "invalid_response",
      );
    data = {};
  }
  if (!response.ok)
    throw new RequestError(
      errorMessage(data?.error),
      response.status,
      data?.code,
    );
  return data;
}
