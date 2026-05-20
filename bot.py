import discord
from discord.ext import commands
import json
import os
from bs4 import BeautifulSoup
from vinted_scraper import VintedScraper
from curl_cffi import requests as requests_cffi
import threading
from flask import Flask
import time
import urllib.parse
import asyncio

# ================= 0. SERVER WEB & KEEP-ALIVE =================
app = Flask('')

@app.route('/')
def home():
    return "Sniper Bot Online e Sveglio!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def self_ping_loop():
    time.sleep(30)
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        render_url = "http://localhost:8080"
    while True:
        try:
            res = requests_cffi.get(render_url, timeout=10)
            print(f"⏰ [KEEP-ALIVE] Ping inviato. Status: {res.status_code}", flush=True)
        except:
            pass
        time.sleep(300)

# ================= 1. CONFIGURAZIONE DATABASE =================
DB_FILE = "bot_data.json"

if os.path.exists(DB_FILE):
    with open(DB_FILE, "r") as f:
        data = json.load(f)
else:
    data = {"channel_id": None, "targets": [], "visti": []}

def salva_data():
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

intents = discord.Intents.default()
intents.message_content = True

class MultiSniperBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands sincronizzati.", flush=True)

    async def on_ready(self):
        print(f"Bot connesso come {self.user}", flush=True)
        # Avviamo il motore di scraping in un thread separato SOLO quando il bot è pronto
        if not hasattr(self, 'scraping_started'):
            self.scraping_started = True
            threading.Thread(target=self.start_scraping_thread, daemon=True).start()

    def start_scraping_thread(self):
        """Loop infinito che gira in background su un thread separato"""
        print("🚀 Thread di Scraping parallelo avviato correttamente.", flush=True)
        while True:
            if data["channel_id"] and data["targets"]:
                try:
                    # Esegue la scansione sincrona senza bloccare Discord
                    self.esegui_scansione_sincrona()
                except Exception as e:
                    print(f"⚠️ Errore nel thread di scraping: {e}", flush=True)
            # Aspetta 5 minuti prima del prossimo giro
            time.sleep(300)

    # ================= ESTRATTORI REALI CON TIMEOUT ISOLATI =================

    def scrape_vinted(self, query):
        try:
            scraper = VintedScraper("https://www.vinted.it")
            return scraper.search({"search_text": query, "order": "newest_first"})
        except Exception as e:
            print(f"⚠️ [VINTED] Errore cookie o DataDome: {e}", flush=True)
            return []

    def scrape_wallapop(self, query):
        """Endpoint mobile consumer alternativo. Molto stabile dai data center."""
        url = "https://api.wallapop.com/api/v3/general/search"
        params = {"keywords": query, "order_by": "newest", "source": "search_box"}
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Device-OS": "ios",
            "Accept": "application/json"
        }
        try:
            response = requests_cffi.get(url, params=params, headers=headers, impersonate="safari_ios", timeout=20)
            if response.status_code == 200:
                print(f"✅ [WALLAPOP] Scansione completata.", flush=True)
                return response.json().get("search_objects", [])
            print(f"❌ [WALLAPOP] Blocco o Errore {response.status_code}", flush=True)
            return []
        except Exception as e:
            print(f"⚠️ [WALLAPOP] Timeout o Errore connessione: {e}", flush=True)
            return []

    def scrape_ebay(self, query):
        """Endpoint API di ricerca aperto di eBay. Zero blocchi 403."""
        url = "https://svcs.ebay.com/services/search/FindingService/v1"
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": "eBaySmar-SniperB-PRD-42f8832a8-124bce88",
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": query,
            "sortOrder": "StartTimeNewest",
            "paginationInput.entriesPerPage": "25"
        }
        items = []
        try:
            response = requests_cffi.get(url, params=params, timeout=20)
            if response.status_code == 200:
                search_res = response.json().get("findItemsByKeywordsResponse", [{}])[0].get("searchResult", [{}])[0]
                for item in search_res.get("item", []):
                    try:
                        title = item.get("title", [""])[0]
                        link = item.get("viewItemURL", [""])[0]
                        price = float(item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0].get("__value__", 0))
                        item_id = item.get("itemId", [""])[0]
                        img = item.get("galleryURL", [None])[0]
                        if title and link and price > 0:
                            items.append({"id": item_id, "title": title, "price": price, "url": link, "image": img})
                    except:
                        continue
                print(f"✅ [EBAY] Scansione completata. Trovati {len(items)} oggetti.", flush=True)
                return items
            print(f"❌ [EBAY] Errore API {response.status_code}", flush=True)
            return []
        except Exception as e:
            print(f"⚠️ [EBAY] Errore di rete API: {e}", flush=True)
            return []

    # ================= LOGICA DI SCANSIONE IN THREAD PARALLELO =================
    def esegui_scansione_sincrona(self):
        visti_set = set(data["visti"])
        channel_id = data["channel_id"]

        for target in data["targets"]:
            query = target["query"]
            max_price = target["max_price"]
            print(f"🕵️ [THREAD] Scansiono: '{query}' (Max: €{max_price})", flush=True)

            # --- VINTED ---
            try:
                vinted_items = self.scrape_vinted(query)
                for item in vinted_items:
                    item_id = f"vinted_{item.id}"
                    if item_id not in visti_set and float(item.price) <= max_price:
                        url = item.url if item.url.startswith("http") else f"https://www.vinted.it{item.url}"
                        img = item.photos[0].url if item.photos else None
                        self.invia_notifica_da_thread(channel_id, item.title, item.price, url, "Vinted", max_price, img)
                        visti_set.add(item_id)
            except:
                pass

            # --- WALLAPOP ---
            try:
                wallapop_items = self.scrape_wallapop(query)
                for item in wallapop_items:
                    if not item.get("id"): continue
                    item_id = f"wallapop_{item['id']}"
                    price = float(item.get("price", {}).get("amount", 9999))
                    if item_id not in visti_set and price <= max_price:
                        url = f"https://it.wallapop.com/item/{item['web_slug']}"
                        img = item.get("images", [{}])[0].get("original") if item.get("images") else None
                        self.invia_notifica_da_thread(channel_id, item.get("title"), price, url, "Wallapop", max_price, img)
                        visti_set.add(item_id)
            except:
                pass

            # --- EBAY ---
            try:
                ebay_items = self.scrape_ebay(query)
                for item in ebay_items:
                    item_id = f"ebay_{item['id']}"
                    if item_id not in visti_set and item["price"] <= max_price:
                        self.invia_notifica_da_thread(channel_id, item["title"], item["price"], item["url"], "eBay", max_price, item["image"])
                        visti_set.add(item_id)
            except:
                pass

        data["visti"] = list(visti_set)
        salva_data()

    def invia_notifica_da_thread(self, channel_id, title, price, url, platform, max_price, img_url):
        """Inietta la coroutine nel thread principale asincrono di Discord in modo sicuro"""
        asyncio.run_coroutine_threadsafe(
            self.invia_embed_discord(channel_id, title, price, url, platform, max_price, img_url), 
            self.loop
        )

    async def invia_embed_discord(self, channel_id, title, price, url, platform, max_price, img_url):
        channel = self.get_channel(channel_id)
        if not channel: return
        colori = {"Vinted": 0x00b4bd, "Wallapop": 0x13c1ac, "eBay": 0xe53238}
        embed = discord.Embed(
            title=f"🚨 NUOVO AFFARE SU {platform.upper()}!",
            description=f"**{title}**",
            url=url,
            color=colori.get(platform, 0x2ecc71)
        )
        embed.add_field(name="Prezzo", value=f"**€ {price}**", inline=True)
        embed.add_field(name="Soglia Impostata", value=f"≤ € {max_price}", inline=True)
        if img_url:
            embed.set_thumbnail(url=img_url)
        try:
            await channel.send(embed=embed)
        except:
            pass

bot = MultiSniperBot()

# ================= 2. SLASH COMMANDS =================
@bot.tree.command(name="set_canale", description="Imposta il canale per ricevere i ping.")
async def set_canale(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data["channel_id"] = interaction.channel_id
    salva_data()
    await interaction.followup.send(f"✅ Canale impostato su <#{interaction.channel_id}>!")

@bot.tree.command(name="aggiungi_target", description="Aggiunge un prodotto da monitorare su tutti i siti.")
async def aggiungi_target(interaction: discord.Interaction, query: str, max_price: float):
    await interaction.response.defer()
    nuovo_target = {"query": query.lower(), "max_price": max_price}
    data["targets"].append(nuovo_target)
    salva_data()
    await interaction.followup.send(f"🔍 Avviato monitoraggio globale per: **{query}** (Prezzo ≤ **€ {max_price}**)")

@bot.tree.command(name="lista_target", description="Mostra la lista dei prodotti.")
async def lista_target(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not data["targets"]:
        await interaction.followup.send("Nessun target impostato.")
        return
    lista_testo = "\n".join([f"• **{t['query']}** (Max: € {t['max_price']})" for t in data["targets"]])
    await interaction.followup.send(f"📋 **Target attivi:**\n{lista_testo}")

@bot.tree.command(name="svuota_target", description="Svuota tutti i prodotti in monitoraggio.")
async def svuota_target(interaction: discord.Interaction):
    await interaction.response.defer()
    data["targets"] = []
    salva_data()
    await interaction.followup.send("🗑️ Tutti i target sono stati rimossi con successo.")

# ================= 3. RUN =================
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    bot.run(TOKEN)