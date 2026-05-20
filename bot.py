import discord
from discord.ext import commands, tasks
import json
import os
from bs4 import BeautifulSoup
from vinted_scraper import VintedScraper
from curl_cffi import requests as requests_cffi
import threading
from flask import Flask
import time
import urllib.parse

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
        except Exception as e:
            print(f"⚠️ [KEEP-ALIVE] Errore auto-ping: {e}", flush=True)
        time.sleep(300)

def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

# ================= 1. CONFIGURAZIONE BOT DISCORD =================
TOKEN = os.environ.get("DISCORD_TOKEN")
SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN") # Il token per distruggere i 403
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
        self.scrpe_loop.start()
        await self.tree.sync()
        print("Slash commands sincronizzati.", flush=True)

    async def on_ready(self):
        print(f"Bot connesso correttamente come {self.user}", flush=True)

    # ================= MOTORI DI SCRAPING BYPASS =================

    def scrape_vinted(self, query):
        try:
            scraper = VintedScraper("https://www.vinted.it")
            return scraper.search({"search_text": query, "order": "newest_first"})
        except Exception as e:
            print(f"⚠️ [VINTED] Errore o blocco DataDome: {e}", flush=True)
            return []

    def scrape_wallapop(self, query):
        """Passa tramite il proxy residenziale di Scrape.do per aggirare Cloudflare"""
        if not SCRAPE_DO_TOKEN:
            print("⚠️ SCRAPE_DO_TOKEN mancante nelle variabili d'ambiente!", flush=True)
            return []
            
        target_url = f"https://api.wallapop.com/api/v3/general/search?keywords={urllib.parse.quote(query)}&filters_source=search_box&order_by=newest"
        # URL di Scrape.do che fa da scudo intermediario
        api_url = f"https://api.scrape.do/?token={SCRAPE_DO_TOKEN}&url={urllib.parse.quote(target_url)}"
        
        try:
            response = requests_cffi.get(api_url, timeout=15)
            if response.status_code == 200:
                print(f"✅ [WALLAPOP] Proxy Residenziale Superato! Analizzo i dati...", flush=True)
                return response.json().get("search_objects", [])
            print(f"❌ [WALLAPOP] Scrape.do ha risposto con codice: {response.status_code}", flush=True)
            return []
        except Exception as e:
            print(f"⚠️ [WALLAPOP] Errore API Scrape.do: {e}", flush=True)
            return []

    def scrape_ebay(self, query):
        """Passa tramite Scrape.do per caricare la pagina HTML di eBay senza 403"""
        if not SCRAPE_DO_TOKEN:
            return []
            
        target_url = f"https://www.ebay.it/sch/i.html?_nkw={urllib.parse.quote(query)}&_sop=10&_ipg=25"
        api_url = f"https://api.scrape.do/?token={SCRAPE_DO_TOKEN}&url={urllib.parse.quote(target_url)}"
        items = []
        try:
            response = requests_cffi.get(api_url, timeout=15)
            if response.status_code != 200:
                print(f"❌ [EBAY] Scrape.do ha risposto con codice: {response.status_code}", flush=True)
                return []
            
            soup = BeautifulSoup(response.text, "html.parser")
            listings = soup.find_all("li", class_=lambda x: x and "s-item" in x)
            
            for listing in listings:
                title_elem = listing.find("div", class_="s-item__title") or listing.find("h3")
                price_elem = listing.find("span", class_="s-item__price")
                link_elem = listing.find("a", class_="s-item__link")
                
                if title_elem and price_elem and link_elem:
                    title = title_elem.text.strip()
                    if "Risultati corrispondenti" in title or "Shop on eBay" in title:
                        continue
                        
                    price_str = price_elem.text.replace("EUR", "").replace(",", ".").replace(" ", "").strip()
                    if "a" in price_str:
                        price_str = price_str.split("a")[0].strip()
                        
                    try:
                        price = float(''.join(c for c in price_str if c.isdigit() or c == '.'))
                        link_pulito = link_elem["href"].split("?")[0]
                        item_id = link_pulito.split("/")[-1]
                        
                        items.append({
                            "id": item_id,
                            "title": title,
                            "price": price,
                            "url": link_pulito,
                            "image": None
                        })
                    except ValueError:
                        continue
            print(f"✅ [EBAY] Scansione completata. Estratti {len(items)} oggetti.", flush=True)
            return items
        except Exception as e:
            print(f"⚠️ [EBAY] Errore Scrape.do: {e}", flush=True)
            return []

    # ================= LOOP DI MONITORAGGIO PRINCIPALE BLINDATO =================
    @tasks.loop(minutes=5)
    async def scrpe_loop(self):
        if not data["channel_id"] or not data["targets"]:
            return

        channel = self.get_channel(data["channel_id"])
        if not channel:
            return

        visti_set = set(data["visti"])

        for target in data["targets"]:
            query = target["query"]
            max_price = target["max_price"]
            print(f"🕵️ Scanning globale per: '{query}' (Soglia: €{max_price})", flush=True)

            # --- 1. PROCESSO VINTED ---
            try:
                vinted_items = self.scrape_vinted(query)
                for item in vinted_items:
                    item_id = f"vinted_{item.id}"
                    if item_id not in visti_set and float(item.price) <= max_price:
                        url = item.url if item.url.startswith("http") else f"https://www.vinted.it{item.url}"
                        img = item.photos[0].url if item.photos else None
                        await self.invia_notifica(channel, item.title, item.price, url, "Vinted", max_price, img)
                        visti_set.add(item_id)
            except Exception as e:
                print(f"❌ [LOOP CRASH] Errore modulo Vinted: {e}", flush=True)

            # --- 2. PROCESSO WALLAPOP ---
            try:
                wallapop_items = self.scrape_wallapop(query)
                for item in wallapop_items:
                    if not item.get("id"): continue
                    item_id = f"wallapop_{item['id']}"
                    if item_id not in visti_set and item["price"] <= max_price:
                        await self.invia_notifica(channel, item["title"], item["price"], item["url"], "Wallapop", max_price, item["image"])
                        visti_set.add(item_id)
            except Exception as e:
                print(f"❌ [LOOP CRASH] Errore modulo Wallapop: {e}", flush=True)

            # --- 3. PROCESSO EBAY ---
            try:
                ebay_items = self.scrape_ebay(query)
                for item in ebay_items:
                    item_id = f"ebay_{item['id']}"
                    if item_id not in visti_set and item["price"] <= max_price:
                        await self.invia_notifica(channel, item["title"], item["price"], item["url"], "eBay", max_price, item["image"])
                        visti_set.add(item_id)
            except Exception as e:
                print(f"❌ [LOOP CRASH] Errore modulo eBay: {e}", flush=True)

        data["visti"] = list(visti_set)
        salva_data()

    async def invia_notifica(self, channel, title, price, url, platform, max_price, img_url=None):
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
        except Exception as e:
            print(f"Errore invio messaggio a Discord: {e}", flush=True)

    @scrpe_loop.before_loop
    async def before_scrape_loop(self):
        await self.wait_until_ready()

bot = MultiSniperBot()

# ================= 3. SLASH COMMANDS =================
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

# ================= 4. RUN =================
if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)