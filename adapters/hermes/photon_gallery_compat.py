"""Narrow, fail-closed addition to the pinned Photon sidecar: grouped image delivery."""
from pathlib import Path
import sys

ANCHOR = '    if (req.url === "/send-attachment") {'
LEGACY_PATCH = '''    // Sotto gallery v1: one native multipart message, no separately-sent captions.
    if (req.url === "/gallery-capability") return ok(res, { galleryVersion: 1 });
    if (req.url === "/send-gallery") {
      const { spaceId, paths, summary } = body || {};
      if (!spaceId || typeof summary !== "string" || !summary || summary.length > 1000 || !Array.isArray(paths) || paths.length !== 4 ||
          paths.some(p => typeof p !== "string" || !p.endsWith(".png"))) {
        return badRequest(res, "spaceId and exactly four PNG paths are required");
      }
      const { group, text } = await import("spectrum-ts");
      const space = await resolveSpace(spaceId);
      const result = await space.send(group(text(summary), ...paths.map(p => attachment(p))));
      return ok(res, { messageId: result?.id || null,
        messageIds: result?.content?.items?.map(item => item.id) || [] });
    }
'''
PATCH = '''    // Sotto gallery v1: one native multipart message, no separately-sent captions.
    // The operation receipt is deliberately content-free: it can reconcile a
    // response lost after provider acceptance without retaining paths, text or recipients.
    const validGalleryDispatch = value => typeof value === "string" && /^[A-Za-z0-9._:-]{1,160}$/.test(value);
    const validGalleryMessageId = value => typeof value === "string" && value.length > 0 && value.length <= 512;
    const sanitizeGalleryReceipt = (dispatchId, value) => {
      if (!value || value.dispatchId !== dispatchId ||
          !["accepted", "unknown", "in_flight", "not_attempted", "rejected"].includes(value.acceptance)) return null;
      const receipt = {dispatchId, acceptance:value.acceptance};
      if (["resolve", "dispatch", "complete"].includes(value.phase)) receipt.phase = value.phase;
      if (Number.isFinite(value.recordedAt)) receipt.recordedAt = value.recordedAt;
      if (["space_unavailable", "provider_error", "missing_message_id"].includes(value.category)) receipt.category = value.category;
      if (value.acceptance === "accepted") {
        if (!validGalleryMessageId(value.messageId)) return null;
        receipt.messageId = value.messageId;
        receipt.messageIds = Array.isArray(value.messageIds) ? value.messageIds.filter(validGalleryMessageId).slice(0, 8) : [];
      }
      return receipt;
    };
    const galleryReceiptDir = nodePath.join(process.env.SOTTO_DATA || "/data", "events", "gallery-receipts");
    const galleryReceiptPath = dispatchId => nodePath.join(galleryReceiptDir, dispatchId + ".json");
    const reserveGalleryReceipt = async dispatchId => {
      const previousMaintenance = globalThis.__sottoGalleryReceiptMaintenance || Promise.resolve();
      let releaseMaintenance;
      globalThis.__sottoGalleryReceiptMaintenance = new Promise(resolve => { releaseMaintenance = resolve; });
      await previousMaintenance.catch(() => {});
      try {
        await fsPromises.mkdir(galleryReceiptDir, {recursive:true, mode:0o700});
        await fsPromises.chmod(galleryReceiptDir, 0o700);
        const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
        const entries = [];
        for (const name of await fsPromises.readdir(galleryReceiptDir)) {
          if (!/^[A-Za-z0-9._:-]{1,160}[.]json$/.test(name)) continue;
          const path = nodePath.join(galleryReceiptDir, name);
          const stat = await fsPromises.stat(path);
          const entryDispatchId = name.slice(0, -5);
          let receipt = null;
          try {
            if (stat.size <= 4096) receipt = sanitizeGalleryReceipt(entryDispatchId,
              JSON.parse(await fsPromises.readFile(path, "utf8")));
          } catch {}
          entries.push({path, dispatchId:entryDispatchId, mtime:stat.mtimeMs,
            acceptance:receipt?.acceptance || "unknown"});
        }
        // Terminal receipts are a seven-day reconciliation aid; the outbox/artifact receipt owns
        // permanent terminal dedupe, so terminal entries may leave earlier under the cap. Unknown,
        // in-flight, invalid and corrupt records are never pruned: exhausting the cap fails closed.
        const activeDispatches = globalThis.__sottoGalleryOperations || new Map();
        const removable = entries.filter(entry => !activeDispatches.has(entry.dispatchId)
            && !["unknown", "in_flight"].includes(entry.acceptance))
          .sort((a, b) => a.mtime - b.mtime);
        const remove = new Set(removable.filter(entry => entry.mtime < cutoff));
        let retained = entries.length - remove.size;
        for (const entry of removable) {
          if (retained < 2048) break;
          if (!remove.has(entry)) { remove.add(entry); retained--; }
        }
        await Promise.all([...remove].map(async entry => {
          try { await fsPromises.unlink(entry.path); }
          catch (error) { if (error?.code !== "ENOENT") throw error; }
        }));
        if (retained >= 2048) throw new Error("gallery receipt capacity exhausted");
        await writeGalleryReceipt(dispatchId, {phase:"resolve", acceptance:"in_flight"});
      } finally {
        releaseMaintenance();
      }
    };
    const readGalleryReceipt = async dispatchId => {
      try {
        const stat = await fsPromises.stat(galleryReceiptPath(dispatchId));
        if (stat.size > 4096) return {exists:true, receipt:null};
        const value = JSON.parse(await fsPromises.readFile(galleryReceiptPath(dispatchId), "utf8"));
        return {exists:true, receipt:sanitizeGalleryReceipt(dispatchId, value)};
      } catch (error) {
        if (error?.code === "ENOENT") return {exists:false, receipt:null};
        if (error instanceof SyntaxError) return {exists:true, receipt:null};
        throw error;
      }
    };
    const writeGalleryReceipt = async (dispatchId, value) => {
      await fsPromises.mkdir(galleryReceiptDir, {recursive:true, mode:0o700});
      await fsPromises.chmod(galleryReceiptDir, 0o700);
      const finalPath = galleryReceiptPath(dispatchId);
      const temporary = finalPath + "." + process.pid + "." + crypto.randomUUID() + ".tmp";
      const handle = await fsPromises.open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(JSON.stringify({dispatchId, recordedAt:Date.now(), ...value}));
        await handle.sync();
      } finally {
        await handle.close();
      }
      await fsPromises.rename(temporary, finalPath);
      const directory = await fsPromises.open(galleryReceiptDir, "r");
      try { await directory.sync(); } finally { await directory.close(); }
    };
    if (req.url === "/gallery-receipt") {
      const { dispatchId } = body || {};
      if (!validGalleryDispatch(dispatchId)) return badRequest(res, "valid dispatchId is required");
      // A running operation is unsettled whatever its last written phase says.
      if (globalThis.__sottoGalleryOperations?.has(dispatchId)) return ok(res, {found:true, receipt:{dispatchId, acceptance:"in_flight"}});
      const found = await readGalleryReceipt(dispatchId);
      return ok(res, found.exists ? {found:true, receipt:found.receipt || {dispatchId, acceptance:"unknown"}} : {found:false});
    }
    if (req.url === "/gallery-capability") return ok(res, { galleryVersion: 1 });
    if (req.url === "/send-gallery") {
      const { dispatchId, spaceId, paths, summary } = body || {};
      if (!validGalleryDispatch(dispatchId) || !spaceId || typeof summary !== "string" || !summary || summary.length > 1000 || !Array.isArray(paths) || paths.length !== 4 ||
          paths.some(p => typeof p !== "string" || !p.endsWith(".png"))) {
        return badRequest(res, "dispatchId, spaceId and exactly four PNG paths are required");
      }
      const active = globalThis.__sottoGalleryOperations ||= new Map();
      let operation = active.get(dispatchId);
      if (!operation) {
        operation = (async () => {
          const found = await readGalleryReceipt(dispatchId);
          const previous = found.receipt;
          if (found.exists && !previous) return {dispatchId, acceptance:"unknown"};
          if (previous && ["accepted", "unknown", "in_flight"].includes(previous.acceptance)) return previous;
          if (!found.exists) await reserveGalleryReceipt(dispatchId);
          const { group, text } = await import("spectrum-ts");
          let space;
          try {
            space = await resolveSpace(spaceId);
          } catch (error) {
            await writeGalleryReceipt(dispatchId, {phase:"resolve", acceptance:"not_attempted", category:"space_unavailable"});
            throw error;
          }
          await writeGalleryReceipt(dispatchId, {phase:"dispatch", acceptance:"in_flight"});
          try {
            const result = await space.send(group(text(summary), ...paths.map(p => attachment(p))));
            const receipt = {phase:"complete", acceptance:"accepted",
              messageId:validGalleryMessageId(result?.id) ? result.id : null,
              messageIds:(result?.content?.items || []).map(item => item.id).filter(validGalleryMessageId).slice(0, 8)};
            if (!receipt.messageId) {
              await writeGalleryReceipt(dispatchId, {phase:"complete", acceptance:"unknown", category:"missing_message_id"});
              return {phase:"complete", acceptance:"unknown", category:"missing_message_id"};
            }
            await writeGalleryReceipt(dispatchId, receipt);
            return receipt;
          } catch (error) {
            await writeGalleryReceipt(dispatchId, {phase:"dispatch", acceptance:"unknown", category:"provider_error"});
            throw error;
          }
        })();
        active.set(dispatchId, operation);
      }
      try {
        const receipt = await operation;
        return ok(res, {ok:receipt.acceptance === "accepted", acceptance:receipt.acceptance,
          messageId:receipt.messageId || null, messageIds:receipt.messageIds || []});
      } finally {
        if (active.get(dispatchId) === operation) active.delete(dispatchId);
      }
    }
'''


def patch(path):
    path = Path(path)
    text = path.read_text()
    if PATCH in text:
        return
    imports = 'import fsPromises from "node:fs/promises";\nimport nodePath from "node:path";\n'
    if 'import fsPromises from "node:fs/promises";' not in text:
        text = imports + text
    if LEGACY_PATCH in text:
        path.write_text(text.replace(LEGACY_PATCH, PATCH))
        return
    if text.count(ANCHOR) != 1 or '"/send-gallery"' in text:
        raise RuntimeError('Photon gallery contract changed; review the runtime pin')
    path.write_text(text.replace(ANCHOR, PATCH + ANCHOR))


if __name__ == '__main__':
    patch(sys.argv[1])
