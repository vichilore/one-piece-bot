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
            print(f"⏰ [KEEP-ALIVE] Ping inviato. Status: {res.status_code}")
        except Exception as e:
            print(f"⚠️ [KEEP-ALIVE] Errore auto-ping: {e}")
        time.sleep(300)

def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

# ================= 1. CONFIGURAZIONE BOT DISCORD =================
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

    # ================= NUOVI MOTORI DI SCRAPING IMMUNI AI BLOCCHI =================

    def scrape_vinted(self, query):
        try:
            scraper = VintedScraper("https://www.vinted.it")
            return scraper.search({"search_text": query, "order": "newest_first"})
        except Exception as e:
            print(f"⚠️ [VINTED] Errore o blocco DataDome: {e}")
            return []

    def scrape_wallapop(self, query):
        """Estrae i dati dal blocco JSON nativo della pagina web, aggirando i blocchi API"""
        url = f"https://it.wallapop.com/app/search?keywords={query.replace(' ', '%20')}&order_by=newest"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"
        }
        items = []
        try:
            response = requests_cffi.get(url, headers=headers, impersonate="chrome", timeout=10)
            if response.status_code != 200:
                print(f"❌ [WALLAPOP] Errore di caricamento pagina: {response.status_code}")
                return []
            
            soup = BeautifulSoup(response.text, "html.parser")
            # Wallapop nasconde tutti i dati degli annunci in questo tag script specifico
            script_tag = soup.find("script", id="__NEXT_DATA__")
            
            if script_tag:
                json_data = json.loads(script_tag.string)
                # Navighiamo all'interno dell'albero JSON di Next.js per trovare i prodotti
                search_results = json_data.get("props", {}).get("pageProps", {}).get("searchResults", {})
                objects = search_results.get("elements", [])
                
                for obj in objects:
                    # Estraiamo solo i dati essenziali normalizzandoli
                    if "id" in obj:
                        items.append({
                            "id": obj["id"],
                            "title": obj.get("title", "Oggetto Wallapop"),
                            "price": float(obj.get("price", 0)),
                            "url": f"https://it.wallapop.com/item/{obj.get('webSlug')}",
                            "image": obj.get("images", [{}])[0].get("original") if obj.get("images") else None
                        })
            return items
        except Exception as e:
            print(f"⚠️ [WALLAPOP] Errore estrazione NEXT_DATA: {e}")
            return []

    def scrape_ebay(self, query):
        """Sfrutta il feed RSS ufficiale di eBay. Niente layout HTML, zero blocchi 403 dalle VPS"""
        url = f"https://www.ebay.it/sch/i.html?_nkw={query.replace(' ', '+')}&_sop=10&_rss=1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        items = []
        try:
            response = requests_cffi.get(url, headers=headers, impersonate="chrome", timeout=10)
            if response.status_code != 200:
                print(f"❌ [EBAY] Errore Feed RSS: {response.status_code}")
                return []
            
            # Utilizziamo il parser XML nativo per estrarre i tag strutturati
            soup = BeautifulSoup(response.text, "xml")
            listings = soup.find_all("item")
            
            for listing in listings:
                title_elem = listing.find("title")
                link_elem = listing.find("link")
                
                # Nei feed RSS di eBay, il prezzo è inserito in modo pulito in questi tag personalizzati
                price_elem = listing.find("g-core:price") or listing.find("price")
                
                if title_elem and link_elem:
                    title = title_elem.text.strip()
                    link = link_elem.text.strip()
                    
                    if "Risultati corrispondenti a meno parole" in title:
                        continue
                    
                    price = 0.0
                    if price_elem:
                        try:
                            price = float(price_elem.text.replace(",", ".").strip())
                        except:
                            continue
                    else:
                        # Fallback se il tag fallisce: lo estraiamo dal testo della descrizione
                        desc = listing.find("description").text if listing.find("description") else ""
                        if "EUR" in desc:
                            try:
                                price_str = desc.split("EUR")[1].split("<")[0].strip().replace(",", ".")
                                price = float(''.join(c for c in price_str if c.isdigit() or c == '.'))
                            except:
                                continue
                    
                    if price > 0:
                        item_id = link.split("/")[-1].split("?")[0]
                        items.append({
                            "id": item_id,
                            "title": title,
                            "price": price,
                            "url": link,
                            "image": None
                        })
            return items
        except Exception as e:
            print(f"⚠️ [EBAY] Errore lettura Feed XML: {e}")
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

            # --- 1. SCAN VINTED ---
            vinted_items = self.scrape_vinted(query)
            for item in vinted_items:
                item_id = f"vinted_{item.id}"
                if item_id not in visti_set and float(item.price) <= max_price:
                    url = item.url if item.url.startswith("http") else f"https://www.vinted.it{item.url}"
                    img = item.photos[0].url if item.photos else None
                    await self.invia_notifica(channel, item.title, item.price, url, "Vinted", max_price, img)
                    visti_set.add(item_id)

            # --- 2. SCAN WALLAPOP ---
            wallapop_items = self.scrape_wallapop(query)
            for item in wallapop_items:
                item_id = f"wallapop_{item['id']}"
                if item_id not in visti_set and item["price"] <= max_price:
                    await self.invia_notifica(channel, item["title"], item["price"], item["url"], "Wallapop", max_price, item["image"])
                    visti_set.add(item_id)

            # --- 3. SCAN EBAY ---
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