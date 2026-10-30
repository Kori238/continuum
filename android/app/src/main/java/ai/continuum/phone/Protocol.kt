package ai.continuum.phone

import org.json.JSONArray
import org.json.JSONObject

/**
 * The wire contract with Continuum's `/phone/ws`.
 *
 * Deliberately hand-rolled on org.json rather than a serialization library:
 * the protocol is a dozen small messages, it has to stay readable next to the
 * Python side that speaks it, and adding kotlinx-serialization would buy
 * type-safety we'd immediately lose at the socket boundary anyway.
 *
 * Server-side counterpart: continuum/app/server.py (`/phone/ws`) and
 * continuum/devices/phone.py.
 */
object Protocol {

    /** Commands this build can actually carry out. The server only asks for
     *  what we advertise here, so this list is the honest capability set —
     *  anything aspirational must stay out of it. */
    val CAPABILITIES = listOf(
        "open_app", "notify", "list_apps", "ui_observe", "global_action", "routine_widget",
        "set_alarm", "list_alarms", "cancel_alarm",
    )

    fun hello(name: String, model: String, os: String): String =
        JSONObject().apply {
            put("type", "hello")
            put("name", name)
            put("model", model)
            put("os", os)
            put("capabilities", JSONArray(CAPABILITIES))
        }.toString()

    fun report(kind: String, payload: JSONObject): String =
        JSONObject().apply {
            put("type", "report")
            put("kind", kind)
            put("payload", payload)
        }.toString()

    fun notification(payload: JSONObject): String =
        JSONObject().apply {
            put("type", "notification")
            put("payload", payload)
        }.toString()

    fun result(corr: String, payload: JSONObject): String =
        JSONObject().apply {
            put("type", "result")
            put("corr", corr)
            put("payload", payload)
        }.toString()

    fun ping(): String = JSONObject().put("type", "ping").toString()

    /** An error is still a result — the server is awaiting this correlation id
     *  and would otherwise sit until its timeout. */
    fun failure(corr: String, reason: String): String =
        result(corr, JSONObject().put("ok", false).put("error", reason))
}
