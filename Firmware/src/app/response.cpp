#include "app/response.h"
#include "app/commands.h"   // jsonOutputMode

String g_requestPath = "";
String g_requestFull = "";

// Render a resolved node: a string scalar is printed bare (unquoted); numbers,
// bools, null, objects and arrays go through serializeJson (numbers/bool/null
// already render without quotes, containers render as JSON).
static void renderNode(JsonVariantConst node) {
  if (node.is<const char *>()) {
    const char *s = node.as<const char *>();
    if (s) Serial.print(s);
  } else {
    serializeJson(node, Serial);
  }
}

// Emit an error object {"error":code,"path":...,"at":...,"available":[...]}.
// `availFrom`, when an object, supplies the sibling keys for "available".
static void emitError(const char *code, const char *at = nullptr,
                      JsonVariantConst availFrom = JsonVariantConst()) {
  JsonDocument err;
  err["error"] = code;
  if (g_requestFull.length()) err["path"] = g_requestFull;
  if (at) err["at"] = at;
  if (availFrom.is<JsonObjectConst>()) {
    JsonArray av = err["available"].to<JsonArray>();
    for (JsonPairConst kv : availFrom.as<JsonObjectConst>())
      av.add(kv.key().c_str());
  }
  serializeJson(err, Serial);
}

// Walk g_requestPath against `root`, then render the resolved node or an error.
static void resolvePath(JsonVariantConst root) {
  JsonVariantConst node = root;
  String lastSeg = "";  // segment that produced `node` (for not_a_container.at)
  const String &path = g_requestPath;
  const int len = path.length();
  int start = 0;

  while (start <= len) {
    const int dot = path.indexOf('.', start);
    String seg = (dot < 0) ? path.substring(start) : path.substring(start, dot);

    if (seg.length() == 0) { emitError("bad_path"); return; }

    // Introspection method, e.g. keys()
    if (seg.endsWith("()")) {
      if (seg == "keys()") {
        if (!node.is<JsonObjectConst>()) {
          emitError("not_a_container", lastSeg.c_str());
          return;
        }
        JsonDocument out;
        JsonArray arr = out.to<JsonArray>();
        for (JsonPairConst kv : node.as<JsonObjectConst>())
          arr.add(kv.key().c_str());
        serializeJson(out, Serial);
        return;
      }
      emitError("bad_path");
      return;
    }

    if (node.is<JsonObjectConst>()) {
      JsonObjectConst obj = node.as<JsonObjectConst>();
      JsonVariantConst child = obj[seg.c_str()];
      if (child.isNull()) { emitError("no_such_key", seg.c_str(), node); return; }
      node = child;
    } else if (node.is<JsonArrayConst>()) {
      JsonArrayConst arr = node.as<JsonArrayConst>();
      bool digits = seg.length() > 0;
      for (int i = 0; i < (int)seg.length(); i++)
        if (!isdigit((unsigned char)seg[i])) { digits = false; break; }
      if (!digits) { emitError("bad_path"); return; }
      const long idx = seg.toInt();
      if (idx < 0 || idx >= (long)arr.size()) {
        emitError("index_out_of_range", seg.c_str());
        return;
      }
      node = arr[(size_t)idx];
    } else {
      emitError("not_a_container", lastSeg.c_str());
      return;
    }

    lastSeg = seg;
    if (dot < 0) break;
    start = dot + 1;
  }

  renderNode(node);
}

void respond(JsonDocument &doc) {
  JsonVariantConst root = doc.as<JsonVariantConst>();
  // Error objects ignore path projection — return the error as-is.
  const bool isError =
      root.is<JsonObjectConst>() && !root.as<JsonObjectConst>()["error"].isNull();

  if (jsonOutputMode || isError || g_requestPath.length() == 0) {
    serializeJson(doc, Serial);
    return;
  }
  resolvePath(root);
}

void respondError(const char *code) {
  JsonDocument doc;
  doc["error"] = code;
  respond(doc);
}
