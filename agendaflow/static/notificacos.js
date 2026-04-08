(function () {
    let permissionAsked = false;

    function el(id) {
        return document.getElementById(id);
    }

    function tocarSomNotificacao() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();

            osc.type = "sine";
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            gain.gain.setValueAtTime(0.001, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);

            osc.connect(gain);
            gain.connect(ctx.destination);

            osc.start();
            osc.stop(ctx.currentTime + 0.35);
        } catch (e) {}
    }

    function mostrarToast(texto) {
        const toast = el("toast-notificacao");
        if (!toast) return;

        toast.textContent = texto;
        toast.classList.add("ativo");

        setTimeout(() => {
            toast.classList.remove("ativo");
        }, 3500);
    }

    async function pedirPermissao() {
        if (!("Notification" in window)) return false;
        if (Notification.permission === "granted") return true;
        if (Notification.permission === "denied") return false;
        if (permissionAsked) return false;

        permissionAsked = true;

        try {
            const result = await Notification.requestPermission();
            return result === "granted";
        } catch (e) {
            return false;
        }
    }

    async function registrarServiceWorker() {
        if ("serviceWorker" in navigator) {
            try {
                await navigator.serviceWorker.register("/static/sw.js");
            } catch (e) {}
        }
    }

    function converterDataBrParaIso(dataBr) {
        if (!dataBr || typeof dataBr !== "string") return "";

        const partes = dataBr.split("/");
        if (partes.length !== 3) return "";

        const [dia, mes, ano] = partes;
        if (!dia || !mes || !ano) return "";

        return `${ano}-${mes.padStart(2, "0")}-${dia.padStart(2, "0")}`;
    }

    function obterDestinoNotificacao(item) {
        const dataIso = converterDataBrParaIso(item.data || "");

        if (dataIso) {
            return `/agenda?data=${encodeURIComponent(dataIso)}`;
        }

        return item.link || "/agenda";
    }

    async function mostrarNotificacaoSistema(titulo, corpo, link) {
        const permitido = await pedirPermissao();
        if (!permitido) return;

        if ("serviceWorker" in navigator) {
            try {
                const reg = await navigator.serviceWorker.getRegistration();
                if (reg) {
                    reg.showNotification(titulo, {
                        body: corpo,
                        icon: "/static/icons/icon-192.png",
                        badge: "/static/icons/icon-192.png",
                        data: { link: link || "/agenda" },
                        tag: "agendaflow-agendamento"
                    });
                    return;
                }
            } catch (e) {}
        }

        try {
            new Notification(titulo, {
                body: corpo,
                icon: "/static/icons/icon-192.png"
            });
        } catch (e) {}
    }

    function render(lista, naoLidas) {
        const badge = el("notif-badge");
        const box = el("notif-list");
        const vazio = el("notif-vazio");

        if (badge) {
            badge.textContent = naoLidas > 99 ? "99+" : String(naoLidas);
            badge.style.display = naoLidas > 0 ? "inline-flex" : "none";
        }

        if (!box) return;

        box.innerHTML = "";

        if (!lista || lista.length === 0) {
            if (vazio) vazio.style.display = "block";
            box.innerHTML = "";
            return;
        }

        if (vazio) vazio.style.display = "none";

        lista.forEach(item => {
            const destino = obterDestinoNotificacao(item);

            const card = document.createElement("div");
            card.className = `notif-item ${item.lida ? "" : "nao-lida"}`;

            const whats = item.whatsapp_link
                ? `<a class="notif-mini-btn whats" href="${item.whatsapp_link}" target="_blank" rel="noopener noreferrer">WhatsApp</a>`
                : "";

            card.innerHTML = `
                <div class="notif-item-topo">
                    <div class="notif-item-titulo">${item.titulo || "Notificação"}</div>
                    ${item.lida ? "" : `<button class="notif-mini-btn" data-lida="${item.id}">Marcar lida</button>`}
                </div>
                <div class="notif-item-texto">${item.mensagem || ""}</div>
                <div class="notif-item-meta">${item.data || ""}${item.hora ? " • " + item.hora : ""}</div>
                <div class="notif-item-acoes">
                    <a class="notif-mini-btn abrir" href="${destino}">Abrir</a>
                    ${whats}
                </div>
            `;

            box.appendChild(card);
        });

        document.querySelectorAll("[data-lida]").forEach(btn => {
            btn.addEventListener("click", async function (e) {
                e.preventDefault();
                e.stopPropagation();

                const id = Number(this.getAttribute("data-lida"));
                if (!id) return;

                try {
                    await fetch(`/notificacoes/${id}/ler`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" }
                    });
                } catch (e) {}

                carregarNotificacoes(false);
            });
        });
    }

    async function carregarNotificacoes(avisoNovo = true) {
        try {
            const resp = await fetch("/verificar_novos", {
                headers: {
                    "Accept": "application/json"
                }
            });

            if (!resp.ok) return;

            const data = await resp.json();

            const lista = data.notificacoes || [];
            const naoLidas = data.nao_lidas || 0;
            const ultimoId = data.ultimo_id || 0;
            const salvo = Number(localStorage.getItem("agendaflow_ultimo_notif_id") || "0");

            render(lista, naoLidas);

            if (avisoNovo && ultimoId > 0 && salvo > 0 && ultimoId !== salvo) {
                const maisNova = lista[0];
                if (maisNova) {
                    const destino = obterDestinoNotificacao(maisNova);

                    mostrarToast("🔔 Novo agendamento recebido");
                    tocarSomNotificacao();
                    mostrarNotificacaoSistema(
                        maisNova.titulo || "Novo agendamento",
                        maisNova.mensagem || "",
                        destino
                    );
                }
            }

            if (ultimoId > 0) {
                localStorage.setItem("agendaflow_ultimo_notif_id", String(ultimoId));
            }
        } catch (e) {}
    }

    function prepararUI() {
        const toggle = el("notif-toggle");
        const dropdown = el("notif-dropdown");

        if (toggle && dropdown) {
            toggle.addEventListener("click", function (e) {
                e.preventDefault();
                dropdown.classList.toggle("aberto");
            });

            document.addEventListener("click", function (e) {
                if (!dropdown.contains(e.target) && !toggle.contains(e.target)) {
                    dropdown.classList.remove("aberto");
                }
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        registrarServiceWorker();
        prepararUI();
        carregarNotificacoes(false);
        setInterval(() => carregarNotificacoes(true), 10000);
    });
})();