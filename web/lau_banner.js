// LAu Studio banner: a frontend-only node that just shows the LAu Studio artwork on the canvas.
// It is never sent to the server (virtual node), so it does not change how the workflow runs.
import { app } from "../../scripts/app.js";

const IMAGE_URL = new URL("./lau_studio_banner.jpg", import.meta.url).href;
const ASPECT = 1792 / 768;

app.registerExtension({
    name: "LAu.StudioBanner",
    registerCustomNodes() {
        class LAuStudioBanner extends LGraphNode {
            static title = "LAu Studio";
            static title_mode = LiteGraph.NO_TITLE;
            static collapsable = false;

            constructor(title) {
                super(title ?? LAuStudioBanner.title);
                this.isVirtualNode = true;
                this.serialize_widgets = false;
                this.properties = this.properties || {};
                if (!this.size || this.size[0] < 100) this.size = [1400, Math.round(1400 / ASPECT)];
                this.img = new Image();
                this.img.onload = () => this.graph?.setDirtyCanvas(true, true);
                this.img.src = IMAGE_URL;
            }

            onDrawForeground(ctx) {
                const img = this.img;
                if (!img?.complete || !img.naturalWidth) return;
                const [w, h] = this.size;
                const s = Math.min(w / img.naturalWidth, h / img.naturalHeight);
                const dw = img.naturalWidth * s, dh = img.naturalHeight * s;
                ctx.save();
                ctx.beginPath();
                ctx.roundRect((w - dw) / 2, (h - dh) / 2, dw, dh, 12);
                ctx.clip();
                ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh);
                ctx.restore();
            }
        }

        LiteGraph.registerNodeType("LAuStudioBanner", LAuStudioBanner);
        LAuStudioBanner.category = "LAu Motion Transfer";
    },
});
