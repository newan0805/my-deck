import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.chainit.LANDeck';
const OBJECT_PATH = '/io/chainit/LANDeck';
const IFACE = 'io.chainit.LANDeck';
const XML = `<node><interface name="${IFACE}">
  <method name="ListWindows"><arg type="s" direction="out" name="windows"/></method>
  <method name="ListWorkspaces"><arg type="s" direction="out" name="workspaces"/></method>
  <method name="ToggleWindow"><arg type="i" direction="in" name="windowId"/><arg type="b" direction="out" name="ok"/></method>
  <method name="SwitchWorkspace"><arg type="i" direction="in" name="index"/><arg type="b" direction="out" name="ok"/></method>
  <method name="CreateWorkspace"><arg type="i" direction="in" name="monitorIndex"/><arg type="b" direction="out" name="ok"/></method>
</interface></node>`;

export default class LanDeckExtension extends Extension {
  enable() {
    const api = {
      ListWindows: () => JSON.stringify(global.get_window_actors().map(actor => {
        const w = actor.meta_window;
        // GNOME's own window -> app mapping (same as the dash), so icons match exactly.
        const app = Shell.WindowTracker.get_default().get_window_app(w);
        return { id: w.get_id(), title: w.get_title() || '', wm_class: w.get_wm_class() || '', minimized: w.minimized, app: app?.get_id() || '' };
      }).filter(w => w.title)),
      ListWorkspaces: () => JSON.stringify({
        count: global.workspace_manager.n_workspaces,
        active: global.workspace_manager.get_active_workspace_index(),
        monitors: Array.from({length: global.display.get_n_monitors()}, (_, i) => `Monitor ${i + 1}`),
      }),
      ToggleWindow: id => {
        const win = global.get_window_actors().map(a => a.meta_window).find(w => w.get_id() === id);
        if (!win)
          return false;
        if (global.display.get_focus_window() === win && !win.minimized)
          win.minimize();
        else
          win.activate(global.get_current_time());
        return true;
      },
      SwitchWorkspace: index => {
        const workspace = global.workspace_manager.get_workspace_by_index(index);
        if (!workspace)
          return false;
        workspace.activate(global.get_current_time());
        return true;
      },
      CreateWorkspace: monitorIndex => {
        const workspace = global.workspace_manager.append_new_workspace(false, global.get_current_time());
        if (!workspace)
          return false;
        workspace.activate(global.get_current_time());
        return true;
      },
    };
    this._dbusObject = Gio.DBusExportedObject.wrapJSObject(XML, api);
    this._dbusObject.export(Gio.DBus.session, OBJECT_PATH);
    this._ownerId = Gio.bus_own_name_on_connection(Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
  }

  disable() {
    if (this._ownerId) {
      Gio.bus_unown_name(this._ownerId);
      this._ownerId = 0;
    }
    if (this._dbusObject) {
      this._dbusObject.unexport();
      this._dbusObject = null;
    }
  }
}
