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
import random

# ================= 0. SERVER WEB & AUTO-PING (KEEP-ALIVE RENDER) =================
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
            print(f"⏰ [KEEP-ALIVE] Ping inviato. Status: {res.status_code}")
        except Exception as e:
            print(f"⚠️ [KEEP-ALIVE] Errore auto-ping: {e}")
        time.sleep(300)

def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

# ================= 1. GESTORE PROXY PUBBLICI ROTANTI =================
def ottieni_lista_proxies():
    """Scarica un blocco di proxy una volta sola per velocizzare le chiamate"""
    url_lista = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=8000&country=all&ssl=all&anonymity=all"
    try:
        response = requests_cffi.get(url_lista, timeout=8)
        if response.status_code == 200 and response.text:
            lista = response.text.strip().split("\r\n")
            if len(lista) < 2:
                lista = response.text.strip().split("\n")
            return [p.strip() for p in lista if p.strip()]
    except Exception as e:
        print(f"⚠️ [PROXY] Errore download lista proxy: {e}")
    return []

# ================= 2. CONFIGURAZIONE BOT DISCORD =================
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

    # ================= MOTORi DI SCRAPING =================

    def scrape_vinted(self, query):
        try:
            scraper = VintedScraper("https://www.vinted.it")
            return scraper.search({"search_text": query, "order": "newest_first"})
        except Exception as e:
            print(f"⚠️ [VINTED] Errore o blocco DataDome: {e}")
            return []

    def scrape_wallapop(self, query, proxy_str):
        url = "https://api.wallapop.com/api/v3/general/search"
        params = {
            "keywords": query,
            "latitude": "41.89193",
            "longitude": "12.51133",
            "order_by": "newest"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Device-OS": "web",
            "Accept": "application/json"
        }
        proxies = {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"} if proxy_str else None
        try:
            response = requests_cffi.get(url, params=params, headers=headers, impersonate="chrome", proxies=proxies, timeout=8)
            if response.status_code == 200:
                return response.json().get("search_objects", [])
            return []
        except:
            return []

    def scrape_ebay(self, query, proxy_str):
        url = f"https://www.ebay.it/sch/i.html?_nkw={query.replace(' ', '+')}&_sop=10&_ipg=25"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }
        proxies = {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"} if proxy_str else None
        items = []
        try:
            response = requests_cffi.get(url, headers=headers, impersonate="chrome", proxies=proxies, timeout=8)
            if response.status_code != 200:
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
        except:
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
        
        # Scarica un pool di proxy freschi all'inizio del ciclo di scansione
        pool_proxies = ottieni_lista_proxies()

        for target in data["targets"]:
            query = target["query"]
            max_price = target["max_price"]
            print(f"🕵️ Scanning: '{query}' (Max: €{max_price}) su Vinted, Wallapop, eBay...")

            # --- 1. SCAN VINTED ---
            vinted_items = self.scrape_vinted(query)
            for item in vinted_items:
                item_id = f"vinted_{item.id}"
                if item_id not in visti_set and float(item.price) <= max_price:
                    url = item.url if item.url.startswith("http") else f"https://www.vinted.it{item.url}"
                    img = item.photos[0].url if item.photos else None
                    await self.invia_notifica(channel, item.title, item.price, url, "Vinted", max_price, img)
                    visti_set.add(item_id)

            # Scegliamo un proxy casuale dal pool per Wallapop ed eBay
            p_wallapop = random.choice(pool_proxies) if pool_proxies else None
            p_ebay = random.choice(pool_proxies) if pool_proxies else None

            # --- 2. SCAN WALLAPOP ---
            wallapop_items = self.scrape_wallapop(query, p_wallapop)
            for item in wallapop_items:
                if not item.get("id"): continue
                item_id = f"wallapop_{item['id']}"
                price = float(item.get("price", {}).get("amount", 9999))
                if item_id not in visti_set and price <= max_price:
                    url = f"https://it.wallapop.com/item/{item['web_slug']}"
                    img = item.get("images", [{}])[0].get("original")
                    await self.invia_notifica(channel, item.get("title"), price, url, "Wallapop", max_price, img)
                    visti_set.add(item_id)

            # --- 3. SCAN EBAY ---
            ebay_items = self.scrape_ebay(query, p_ebay)
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

# ================= 3. SLASH COMMANDS CORAZZATI (CON DEFER) =================
@bot.tree.command(name="set_canale", description="Imposta il canale per ricevere i ping.")
async def set_canale(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True) # Dice a Discord di aspettare, prevenendo il timeout
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