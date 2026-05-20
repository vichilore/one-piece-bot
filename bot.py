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

# ================= 0. SERVER WEB & AUTO-PING (KEEP-ALIVE RENDER) =================
app = Flask('')

@app.route('/')
def home():
    return "Sniper Bot Online e Sveglio!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def self_ping_loop():
    """Invia una richiesta HTTP all'URL del bot stesso per evitare lo sleep di Render"""
    # Aspettiamo 30 secondi all'avvio per dare tempo al server di alzarsi
    time.sleep(30)
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    
    if not render_url:
        print("⚠️ RENDER_EXTERNAL_URL non configurata. L'auto-ping locale simulerà i passaggi.")
        render_url = "http://localhost:8080"

    while True:
        try:
            # Mandiamo un ping web ogni 5 minuti (300 secondi)
            res = requests_cffi.get(render_url, timeout=10)
            print(f"⏰ [KEEP-ALIVE] Ping inviato con successo a {render_url}. Status: {res.status_code}")
        except Exception as e:
            print(f"⚠️ [KEEP-ALIVE] Errore durante l'auto-ping: {e}")
        time.sleep(300)

def keep_alive():
    # Thread per il server Flask
    t_flask = threading.Thread(target=run_flask)
    t_flask.daemon = True
    t_flask.start()
    
    # Thread per l'auto-ping continuo
    t_ping = threading.Thread(target=self_ping_loop)
    t_ping.daemon = True
    t_ping.start()

# ================= 1. DATABASE E CONFIGURAZIONE BOT =================
TOKEN = os.environ.get("DISCORD_TOKEN")
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
        print("Slash commands sincronizzati.")

    async def on_ready(self):
        print(f"Bot connesso correttamente come {self.user}")

    # ================= LOOPS DI SCRAPING SPECIFICI =================

    def scrape_vinted(self, query):
        try:
            scraper = VintedScraper("https://www.vinted.it")
            return scraper.search({"search_text": query, "order": "newest_first"})
        except Exception as e:
            print(f"⚠️ [VINTED] Errore o blocco DataDome: {e}")
            return []

    def scrape_wallapop(self, query):
        url = "https://api.wallapop.com/api/v3/general/search"
        params = {
            "keywords": query,
            "latitude": "41.89193",
            "longitude": "12.51133",
            "order_by": "newest"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Device-OS": "web"
        }
        try:
            response = requests_cffi.get(url, params=params, headers=headers, impersonate="chrome", timeout=10)
            if response.status_code == 200:
                return response.json().get("search_objects", [])
            print(f"❌ debug [WALLAPOP]: Errore HTTP {response.status_code}")
            return []
        except Exception as e:
            print(f"⚠️ [WALLAPOP] Errore API: {e}")
            return []

    def scrape_ebay(self, query):
        url = f"https://www.ebay.it/sch/i.html?_nkw={query.replace(' ', '+')}&_sop=10&_ipg=25"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        items = []
        try:
            response = requests_cffi.get(url, headers=headers, impersonate="chrome", timeout=10)
            if response.status_code != 200:
                print(f"❌ debug [EBAY]: Errore HTTP {response.status_code}")
                return []
            
            soup = BeautifulSoup(response.text, "html.parser")
            listings = soup.find_all("li", class_=lambda x: x and "s-item" in x)
            
            for listing in listings:
                title_elem = listing.find("div", class_="s-item__title")
                price_elem = listing.find("span", class_="s-item__price")
                link_elem = listing.find("a", class_="s-item__link")
                img_elem = listing.find("img")
                
                if title_elem and price_elem and link_elem:
                    title = title_elem.text.strip()
                    if "Risultati corrispondenti a meno parole" in title or "🤖" in title or "Shop on eBay" in title: 
                        continue
                    
                    price_str = price_elem.text.replace("EUR", "").replace(",", ".").replace(" ", "").strip()
                    if "a" in price_str: 
                        price_str = price_str.split("a")[0].strip()
                    
                    try:
                        price = float(''.join(c for c in price_str if c.isdigit() or c == '.'))
                        item_id = link_elem["href"].split("?")[0].split("/")[-1]
                        items.append({
                            "id": item_id,
                            "title": title,
                            "price": price,
                            "url": link_elem["href"].split("?")[0],
                            "image": img_elem["src"] if img_elem else None
                        })
                    except ValueError:
                        continue
            return items
        except Exception as e:
            print(f"⚠️ [EBAY] Errore Scraping: {e}")
            return []

    # ================= LOOP DI MONITORAGGIO PRINCIPALE =================
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
            print(f"🕵️ Scanning: '{query}' (Max: €{max_price}) su Vinted, Wallapop, eBay...")

            # --- VINTED ---
            vinted_items = self.scrape_vinted(query)
            for item in vinted_items:
                item_id = f"vinted_{item.id}"
                if item_id not in visti_set and float(item.price) <= max_price:
                    url = item.url if item.url.startswith("http") else f"https://www.vinted.it{item.url}"
                    img = item.photos[0].url if item.photos else None
                    await self.invia_notifica(channel, item.title, item.price, url, "Vinted", max_price, img)
                    visti_set.add(item_id)

            # --- WALLAPOP ---
            wallapop_items = self.scrape_wallapop(query)
            for item in wallapop_items:
                if not item.get("id"): continue
                item_id = f"wallapop_{item['id']}"
                price = float(item.get("price", {}).get("amount", 9999))
                if item_id not in visti_set and price <= max_price:
                    url = f"https://it.wallapop.com/item/{item['web_slug']}"
                    img = item.get("images", [{}])[0].get("original")
                    await self.invia_notifica(channel, item.get("title"), price, url, "Wallapop", max_price, img)
                    visti_set.add(item_id)

            # --- EBAY ---
            ebay_items = self.scrape_ebay(query)
            for item in ebay_items:
                item_id = f"ebay_{item['id']}"
                if item_id not in visti_set and item["price"] <= max_price:
                    await self.invia_notifica(channel, item["title"], item["price"], item["url"], "eBay", max_price, item["image"])
                    visti_set.add(item_id)

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
            print(f"Errore invio messaggio a Discord: {e}")

    @scrpe_loop.before_loop
    async def before_scrape_loop(self):
        await self.wait_until_ready()

bot = MultiSniperBot()

# ================= 2. SLASH COMMANDS =================
@bot.tree.command(name="set_canale", description="Imposta il canale per ricevere i ping.")
async def set_canale(interaction: discord.Interaction):
    data["channel_id"] = interaction.channel_id
    salva_data()
    await interaction.response.send_message(f"✅ Canale impostato su <#{interaction.channel_id}>!", ephemeral=True)

@bot.tree.command(name="aggiungi_target", description="Aggiunge un prodotto da monitorare su tutti i siti.")
async def aggiungi_target(interaction: discord.Interaction, query: str, max_price: float):
    nuovo_target = {"query": query.lower(), "max_price": max_price}
    data["targets"].append(nuovo_target)
    salva_data()
    await interaction.response.send_message(f"🔍 Avviato monitoraggio globale per: **{query}** (Prezzo ≤ **€ {max_price}**)")

@bot.tree.command(name="lista_target", description="Mostra la lista dei prodotti.")
async def lista_target(interaction: discord.Interaction):
    if not data["targets"]:
        await interaction.response.send_message("Nessun target impostato.", ephemeral=True)
        return
    lista_testo = "\n".join([f"• **{t['query']}** (Max: € {t['max_price']})" for t in data["targets"]])
    await interaction.response.send_message(f"📋 **Target attivi:**\n{lista_testo}", ephemeral=True)

@bot.tree.command(name="svuota_target", description="Svuota tutti i prodotti in monitoraggio.")
async def svuota_target(interaction: discord.Interaction):
    data["targets"] = []
    salva_data()
    await interaction.response.send_message("🗑️ Tutti i target sono stati rimossi con successo.")

# ================= 3. RUN =================
if __name__ == "__main__":
    keep_alive()  # Fa partire Flask e l'Auto-Ping nei thread in background
    bot.run(TOKEN)