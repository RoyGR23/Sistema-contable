# main.py actualizado
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from decimal import Decimal
from supabase import create_client, Client
import os
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
import hashlib
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    # Pre-hash con SHA-256 para prevenir el limite de 72-bytes de bcrypt
    sha_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    return pwd_context.hash(sha_hash)

def verify_password(plain_password, hashed_password):
    sha_hash = hashlib.sha256(plain_password.encode('utf-8')).hexdigest()
    return pwd_context.verify(sha_hash, hashed_password)

app = FastAPI(title="API Sistema Contable - Tienda de Ropa")

@app.api_route("/api/v1/ping", methods=["GET", "HEAD"])
def despertar_servidor():
    return {"estado": "Despierto", "mensaje": "El servidor de contabilidad está activo y listo."}

# Permisos a React
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite cualquier origen (dev + producción en Render)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Conexión real a tu base de datos Supabase
# En producción, estas claves deben estar en variables de entorno (.env)
URL_SUPABASE = os.environ.get("SUPABASE_URL")
CLAVE_SUPABASE = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(URL_SUPABASE, CLAVE_SUPABASE)

class ArticuloVenta(BaseModel):
    variante_id: str
    cantidad: int
    precio_unitario: float # Usamos float en la petición para facilitar JSON, lo pasamos a Decimal en la lógica

class PeticionFactura(BaseModel):
    cliente_id: Optional[str] = None
    rnc_cliente: Optional[str] = None
    nombre_cliente: Optional[str] = None
    tipo_comprobante: str
    metodo_pago: str
    articulos: List[ArticuloVenta]
    descuento_id: Optional[str] = None
    descuento: Optional[float] = 0.0
    tipo_venta: str = "Contado"
    aplicar_itbis: bool = True

class ClienteBase(BaseModel):
    rnc_cedula: str
    nombre_cliente: str
    telefono: Optional[str] = None
    email: Optional[str] = None
    direccion: Optional[str] = None

class ProductoBase(BaseModel):
    nombre: str
    descripcion: Optional[str] = None
    precio_base: float
    graba_itbis: bool = True

class VarianteBase(BaseModel):
    producto_id: str
    talla: str
    color: str
    sku: str
    codigo_barras: Optional[str] = None
    precio_modificado: Optional[float] = None

class InventarioBase(BaseModel):
    variante_id: str
    ubicacion: str = "Tienda Principal - La Vega"
    cantidad_disponible: int

class InventarioUpdate(BaseModel):
    producto_id: str
    variante_id: str
    prenda: str
    color: str
    talla: str
    precio: float
    stock: int
    sku: str

class TipoPrendaBase(BaseModel):
    nombre: str

class ColorBase(BaseModel):
    nombre: str

class TallaBase(BaseModel):
    nombre: str

class DescuentoBase(BaseModel):
    nombre_descuento: str
    tipo: str # 'P' para porcentaje, 'R' para resta
    valor_descuento: float
    usuario_creador: str
    cliente_id: Optional[str] = None

# --- MODELOS DE CONTROL DE ACCESO ---
class RolBase(BaseModel):
    nombre: str
    descripcion: Optional[str] = None

class UsuarioBase(BaseModel):
    nombre_completo: str
    email: str
    rol_id: str
    activo: bool = True

class UsuarioCreate(UsuarioBase):
    password: str

class UsuarioUpdate(BaseModel):
    nombre_completo: Optional[str] = None
    email: Optional[str] = None
    rol_id: Optional[str] = None
    activo: Optional[bool] = None
    password: Optional[str] = None

class PermisoRolBase(BaseModel):
    modulo_id: str
    puede_ver: bool = False
    puede_crear: bool = False
    puede_editar: bool = False
    puede_eliminar: bool = False

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/v1/facturar")
def procesar_venta(factura: PeticionFactura):
    # 1. Validación de la DGII
    if factura.tipo_comprobante == "B01" and not factura.rnc_cliente:
        raise HTTPException(status_code=400, detail="RNC obligatorio para Crédito Fiscal.")

    # ITBIS y Totales
    subtotal = sum(art.cantidad * art.precio_unitario for art in factura.articulos)
    total_itbis = subtotal * 0.18 if factura.aplicar_itbis else 0.0
    total_pagar = subtotal + total_itbis - factura.descuento

    try:
        # 2. Obtener y actualizar el NCF en Supabase
        # Buscamos la secuencia activa para el tipo de comprobante (Ej. B02)
        res_ncf = supabase.table("secuencias_ncf").select("*").eq("tipo_comprobante", factura.tipo_comprobante).eq("activa", True).execute()
        
        if not res_ncf.data:
            raise Exception(f"No hay secuencias activas para {factura.tipo_comprobante}")
            
        secuencia_data = res_ncf.data[0]
        numero_actual = secuencia_data['secuencia_actual'] + 1
        ncf_generado = f"{secuencia_data['serie']}{str(numero_actual).zfill(8)}" # Ej: B0200000016

        # Actualizamos la tabla para que la próxima factura use el siguiente número
        supabase.table("secuencias_ncf").update({"secuencia_actual": numero_actual}).eq("id", secuencia_data['id']).execute()

        # 3. Crear la factura (Cabecera)
        datos_nueva_factura = {
            "ncf": ncf_generado,
            "rnc_cliente": factura.rnc_cliente,
            "nombre_cliente": factura.nombre_cliente,
            "subtotal": subtotal,
            "descuento": factura.descuento,
            "total_itbis": total_itbis,
            "total_pagar": total_pagar,
            "metodo_pago": factura.metodo_pago
        }
        res_factura = supabase.table("facturas").insert(datos_nueva_factura).execute()
        factura_id = res_factura.data[0]['id']

        # 4. Registrar detalles y descontar inventario
        for articulo in factura.articulos:
            # Insertar en detalles_factura
            supabase.table("detalles_factura").insert({
                "factura_id": factura_id,
                "variante_id": articulo.variante_id,
                "cantidad": articulo.cantidad,
                "precio_unitario": articulo.precio_unitario,
                "monto_itbis": (articulo.precio_unitario * articulo.cantidad * 0.18) if factura.aplicar_itbis else 0.0
            }).execute()

            # Descontar el inventario
            # Primero buscamos cuánto hay
            res_inv = supabase.table("inventario").select("cantidad_disponible").eq("variante_id", articulo.variante_id).execute()
            cantidad_actual = res_inv.data[0]['cantidad_disponible']
            nueva_cantidad = cantidad_actual - articulo.cantidad
            
            # Actualizamos la nueva cantidad
            supabase.table("inventario").update({"cantidad_disponible": nueva_cantidad}).eq("variante_id", articulo.variante_id).execute()

        # 5. Si es Venta a Crédito, insertar en cuentas_por_cobrar
        if factura.tipo_venta == "Credito":
            if not factura.cliente_id:
                raise HTTPException(status_code=400, detail="El cliente es obligatorio para ventas a crédito.")
            
            import datetime
            fecha_venc = datetime.date.today() + datetime.timedelta(days=30)
            
            supabase.table("cuentas_por_cobrar").insert({
                "factura_id": factura_id,
                "cliente_id": factura.cliente_id,
                "monto_inicial": total_pagar,
                "saldo_pendiente": total_pagar,
                "fecha_vencimiento": fecha_venc.isoformat(),
                "estado": "Pendiente"
            }).execute()

        # 6. Responder
        return {
            "estado": "Exito",
            "mensaje": "Venta procesada con éxito",
            "factura": datos_nueva_factura,
            "factura_id": factura_id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la base de datos: {str(e)}")

@app.get("/api/v1/transacciones")
def obtener_transacciones():
    """
    Obtiene el historial de todas las facturas (transacciones) y 
    busca relacionarlas con su cliente respectivo.
    """
    try:
        # Hacemos un select pidiendo la información básica y el nombre del cliente
        respuesta = supabase.table("facturas").select("*, clientes(nombre_cliente)").order("fecha_emision", desc=True).execute()
        
        transacciones_listas = []
        for f in respuesta.data:
            cliente_info = f.get("clientes")
            
            nombre_factura = f.get("nombre_cliente")
            if nombre_factura:
                nombre_cliente = nombre_factura
            elif cliente_info and cliente_info.get("nombre_cliente"):
                nombre_cliente = cliente_info.get("nombre_cliente")
            else:
                nombre_cliente = "No especificado"

            transacciones_listas.append({
                "id": f.get("id"),
                "fecha": f.get("fecha_emision"),
                "ncf": f.get("ncf"),
                "rnc_cliente": f.get("rnc_cliente"),
                "nombre_cliente": nombre_cliente,
                "subtotal": f.get("subtotal", 0),
                "total_itbis": f.get("total_itbis", 0),
                "total_pagar": f.get("total_pagar", 0),
                "metodo_pago": f.get("metodo_pago", "No especificado")
            })

        return {"estado": "Exito", "datos": transacciones_listas}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al cargar transacciones: {str(e)}")

@app.get("/api/v1/dashboard/ventas-mes")
def obtener_ventas_mes():
    try:
        # Suponemos que puede haber una columna 'fecha_emision', pero pedimos todo en caso de que se llame 'created_at' o similar por default.
        respuesta = supabase.table("facturas").select("*").execute()
        
        meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        ventas_dict = {m: {"Ventas": 0.0, "VentasSinITBIS": 0.0} for m in meses}
        
        for f in respuesta.data:
            # Primero buscamos fecha_emision, luego created_at
            fecha_str = f.get("fecha_emision") or f.get("created_at")
            
            if not fecha_str:
                continue
                
            total = f.get("total_pagar", 0) or 0
            subtotal = f.get("subtotal", 0) or 0
            
            try:
                # Convertimos '2025-03-05...' a fecha
                dt = datetime.strptime(fecha_str[:10], "%Y-%m-%d")
                mes_nombre = meses[dt.month - 1]
                ventas_dict[mes_nombre]["Ventas"] += float(total)
                ventas_dict[mes_nombre]["VentasSinITBIS"] += float(subtotal)
            except Exception:
                pass
                
        datos_listos = [{"name": m, "Ventas": ventas_dict[m]["Ventas"], "VentasSinITBIS": ventas_dict[m]["VentasSinITBIS"]} for m in meses]
        return {"estado": "Exito", "datos": datos_listos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/dashboard/clientes-total")
def obtener_total_clientes():
    try:
        # Supabase permite contar registros
        respuesta = supabase.table("clientes").select("rnc_cedula", count="exact").execute()
        return {"estado": "Exito", "total_clientes": respuesta.count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/catalogo")
def obtener_catalogo():
    """
    Busca toda la ropa disponible en la base de datos y la prepara
    para que React pueda mostrarla como botones en la caja registradora.
    """
    try:
        # Supabase nos permite hacer un "Join" facilísimo. 
        # Buscamos las variantes y le pedimos que traiga los datos de la tabla 'productos'
        respuesta = supabase.table("variantes_producto").select(
            "id, talla, color, precio_modificado, productos(nombre, precio_base)"
        ).execute()
        
        catalogo_listo = []
        
        for item in respuesta.data:
            producto_padre = item['productos']
            # Si la talla XL es más cara usa ese precio, si no, usa el precio normal
            precio_final = item['precio_modificado'] if item['precio_modificado'] else producto_padre['precio_base']
            
            catalogo_listo.append({
                "variante_id": item['id'],
                "nombre_mostrar": f"{producto_padre['nombre']} - Talla {item['talla']} ({item['color']})",
                "precio": precio_final
            })
            
        return {"estado": "Exito", "datos": catalogo_listo}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al leer el catálogo: {str(e)}")  
        
# --- ENDPOINTS DE CLIENTES ---

@app.get("/api/v1/clientes")
def obtener_clientes():
    try:
        respuesta = supabase.table("clientes").select("*").order("creado_en", desc=True).execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/clientes")
def crear_cliente(cliente: ClienteBase):
    try:
        # Pydantic a dict
        datos_cliente = cliente.dict()
        respuesta = supabase.table("clientes").insert(datos_cliente).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        # Supabase devuelve un error si el rnc_cedula ya existe (si tiene restricción UNIQUE)
        raise HTTPException(status_code=400, detail=f"No se pudo crear el cliente: {str(e)}")

@app.delete("/api/v1/clientes/{rnc_cedula}")
def eliminar_cliente(rnc_cedula: str):
    try:
        respuesta = supabase.table("clientes").delete().eq("rnc_cedula", rnc_cedula).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        return {"estado": "Exito", "mensaje": "Cliente eliminado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/clientes/{rnc_cedula}")
def actualizar_cliente(rnc_cedula: str, cliente: ClienteBase):
    try:
        datos_actualizar = {
            "nombre_cliente": cliente.nombre_cliente,
            "telefono": cliente.telefono,
            "email": cliente.email,
            "direccion": cliente.direccion
            # No actualizamos rnc_cedula porque es la llave primaria en este contexto
        }
        respuesta = supabase.table("clientes").update(datos_actualizar).eq("rnc_cedula", rnc_cedula).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error al actualizar: {str(e)}")

@app.get("/api/v1/clientes/{rnc_cedula}/compras")
def obtener_compras_cliente(rnc_cedula: str):
    try:
        respuesta = supabase.table("facturas").select("total_pagar").eq("rnc_cliente", rnc_cedula).execute()
        compras = respuesta.data
        if not compras:
            return {"estado": "Exito", "ha_comprado": False, "total_compras": 0.0}
            
        total = sum(f.get("total_pagar", 0) for f in compras)
        return {"estado": "Exito", "ha_comprado": True, "total_compras": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/clientes/{rnc_cedula}/transacciones")
def obtener_transacciones_cliente(rnc_cedula: str, fecha_inicio: Optional[str] = None, fecha_fin: Optional[str] = None):
    """
    Retorna la lista de transacciones (facturas) individuales para un cliente específico.
    Permite filtrar opcionalmente por un rango de fechas.
    """
    try:
        query = supabase.table("facturas").select("*").eq("rnc_cliente", rnc_cedula)
        
        if fecha_inicio:
            # Agregamos hora 00:00:00 para cubrir todo el día
            query = query.gte("fecha_emision", f"{fecha_inicio}T00:00:00")
        if fecha_fin:
            # Agregamos hora 23:59:59 para cubrir todo el día
            query = query.lte("fecha_emision", f"{fecha_fin}T23:59:59")
            
        respuesta = query.order("fecha_emision", desc=True).execute()
        
        transacciones_listas = []
        for f in respuesta.data:
            transacciones_listas.append({
                "id": f.get("id"),
                "fecha": f.get("fecha_emision"),
                "ncf": f.get("ncf"),
                "subtotal": f.get("subtotal", 0),
                "total_itbis": f.get("total_itbis", 0),
                "total_pagar": f.get("total_pagar", 0),
                "metodo_pago": f.get("metodo_pago", "No especificado")
            })

        return {"estado": "Exito", "datos": transacciones_listas}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- ENDPOINTS DE INVENTARIO ---

@app.get("/api/v1/inventario")
def obtener_inventario():
    try:
        # Join de inventario -> variantes_producto -> productos
        respuesta = supabase.table("inventario").select(
            "id, cantidad_disponible, ubicacion, variante_id, variantes_producto(id, talla, color, sku, codigo_barras, precio_modificado, productos(id, nombre, descripcion, precio_base, graba_itbis))"
        ).execute()
        
        datos_listos = []
        for inv in respuesta.data:
            variante = inv.get("variantes_producto") or {}
            producto = variante.get("productos") or {}
            
            # Si no hay variante o producto, evitamos errores saltando al siguiente
            if not variante or not producto:
                continue
            
            precio_final = variante.get("precio_modificado")
            if precio_final is None:
                precio_final = producto.get("precio_base")
                
            datos_listos.append({
                "inventario_id": inv.get("id"),
                "variante_id": variante.get("id"),
                "producto_id": producto.get("id"),
                "nombre": producto.get("nombre"),
                "descripcion": producto.get("descripcion"),
                "talla": variante.get("talla"),
                "color": variante.get("color"),
                "sku": variante.get("sku"),
                "codigo_barras": variante.get("codigo_barras"),
                "precio": precio_final,
                "graba_itbis": producto.get("graba_itbis"),
                "cantidad_disponible": inv.get("cantidad_disponible"),
                "ubicacion": inv.get("ubicacion")
            })
            
        return {"estado": "Exito", "datos": datos_listos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/productos")
def crear_producto(producto: ProductoBase):
    try:
        respuesta = supabase.table("productos").insert(producto.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando producto: {str(e)}")

@app.post("/api/v1/variantes")
def crear_variante(variante: VarianteBase):
    try:
        # 1. Validar que el SKU no exista previamente
        res_sku = supabase.table("variantes_producto").select("id").eq("sku", variante.sku).execute()
        if res_sku.data:
            raise HTTPException(status_code=400, detail="El SKU ya existe en el inventario.")
            
        # 2. Insertar variante si el SKU es único
        respuesta = supabase.table("variantes_producto").insert(variante.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except HTTPException as he:
        # Relanzamos las excepciones HTTP para que FastAPI las maneje correctamente con su código de estado
        raise he
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando variante: {str(e)}")

@app.post("/api/v1/inventario/stock")
def crear_inventario_stock(inventario: InventarioBase):
    try:
        respuesta = supabase.table("inventario").insert(inventario.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error registrando inventario: {str(e)}")

@app.put("/api/v1/inventario/{inventario_id}")
def actualizar_inventario_completo(inventario_id: str, datos: InventarioUpdate):
    try:
        # 1. Update Producto
        res_prod = supabase.table("productos").update({
            "nombre": datos.prenda,
            "descripcion": f"Producto de la categoría {datos.prenda}",
            "precio_base": datos.precio
        }).eq("id", datos.producto_id).execute()
        
        # 2. Update Variante
        res_var = supabase.table("variantes_producto").update({
            "talla": datos.talla,
            "color": datos.color,
            "sku": datos.sku,
            "precio_modificado": datos.precio
        }).eq("id", datos.variante_id).execute()
        
        # 3. Update Inventario (Stock)
        res_inv = supabase.table("inventario").update({
            "cantidad_disponible": datos.stock
        }).eq("id", inventario_id).execute()

        if not res_inv.data:
            raise HTTPException(status_code=404, detail="Inventario no encontrado")

        return {"estado": "Exito", "mensaje": "Inventario actualizado correctamente"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error actualizando inventario: {str(e)}")

@app.delete("/api/v1/inventario/{inventario_id}")
def eliminar_inventario(inventario_id: str):
    try:
        # Buscamos la variante para borrarla en cascada si es posible
        res_inv = supabase.table("inventario").select("variante_id").eq("id", inventario_id).execute()
        if not res_inv.data:
             raise HTTPException(status_code=404, detail="Inventario no encontrado")
        
        variante_id = res_inv.data[0]["variante_id"]
        
        respuesta = supabase.table("inventario").delete().eq("id", inventario_id).execute()
        
        # Opcional intentando borrar la variante. Si la DB tiene CASCADE esto puede no hacer falta.
        try:
             supabase.table("variantes_producto").delete().eq("id", variante_id).execute()
        except Exception:
             pass # Si no se puede por dependencias previas, obviamos
             
        return {"estado": "Exito", "mensaje": "Inventario eliminado permanentemente"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- ENDPOINTS DE CATEGORÍAS (MANTENIMIENTO) ---

@app.get("/api/v1/categorias/prendas")
def obtener_prendas():
    try:
        respuesta = supabase.table("tipos_prenda").select("*").execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/categorias/prendas")
def crear_prenda(prenda: TipoPrendaBase):
    try:
        respuesta = supabase.table("tipos_prenda").insert(prenda.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/v1/categorias/prendas/{id}")
def eliminar_prenda(id: str):
    try:
        respuesta = supabase.table("tipos_prenda").delete().eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Tipo de prenda no encontrado")
        return {"estado": "Exito", "mensaje": "Tipo de prenda eliminado"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/categorias/colores")
def obtener_colores():
    try:
        respuesta = supabase.table("colores").select("*").execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/categorias/colores")
def crear_color(color: ColorBase):
    try:
        respuesta = supabase.table("colores").insert(color.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/v1/categorias/colores/{id}")
def eliminar_color(id: str):
    try:
        respuesta = supabase.table("colores").delete().eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Color no encontrado")
        return {"estado": "Exito", "mensaje": "Color eliminado"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/categorias/tallas")
def obtener_tallas():
    try:
        respuesta = supabase.table("tallas").select("*").execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/categorias/tallas")
def crear_talla(talla: TallaBase):
    try:
        respuesta = supabase.table("tallas").insert(talla.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/v1/categorias/tallas/{id}")
def eliminar_talla(id: str):
    try:
        respuesta = supabase.table("tallas").delete().eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Talla no encontrada")
        return {"estado": "Exito", "mensaje": "Talla eliminada"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- ENDPOINTS DE DESCUENTOS ---

@app.get("/api/v1/descuentos")
def obtener_descuentos():
    try:
        # Seleccionamos todo sobre el descuento y traemos el nombre_cliente asociado (opcional)
        respuesta = supabase.table("descuentos").select("*, clientes(nombre_cliente)").order("creado_en", desc=True).execute()
        
        datos_listos = []
        for d in respuesta.data:
            cliente_info = d.get("clientes")
            nombre_cliente = cliente_info.get("nombre_cliente") if cliente_info else "Todos los clientes"
            
            datos_listos.append({
                "id": d.get("id"),
                "nombre_descuento": d.get("nombre_descuento"),
                "tipo": d.get("tipo"),
                "valor_descuento": d.get("valor_descuento"),
                "usuario_creador": d.get("usuario_creador"),
                "cliente_id": d.get("cliente_id"),
                "nombre_cliente": nombre_cliente,
                "creado_en": d.get("creado_en")
            })
            
        return {"estado": "Exito", "datos": datos_listos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/descuentos")
def crear_descuento(descuento: DescuentoBase):
    try:
        datos_insertar = descuento.dict()
        respuesta = supabase.table("descuentos").insert(datos_insertar).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando descuento: {str(e)}")

@app.put("/api/v1/descuentos/{id}")
def actualizar_descuento(id: str, descuento: DescuentoBase):
    try:
        # Extraemos los datos omitiendo los campos que no deben modificarse
        datos_actualizar = descuento.dict(exclude={"usuario_creador"})
        
        respuesta = supabase.table("descuentos").update(datos_actualizar).eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Descuento no encontrado")
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error actualizando descuento: {str(e)}")

# --- ENDPOINTS DE CONTROL DE ACCESO ---

# Roles
@app.get("/api/v1/roles")
def obtener_roles():
    try:
        respuesta = supabase.table("roles").select("*").execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/roles")
def crear_rol(rol: RolBase):
    try:
        respuesta = supabase.table("roles").insert(rol.dict()).execute()
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando rol: {str(e)}")

@app.put("/api/v1/roles/{id}")
def actualizar_rol(id: str, rol: RolBase):
    try:
        respuesta = supabase.table("roles").update(rol.dict()).eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Rol no encontrado")
        return {"estado": "Exito", "datos": respuesta.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/v1/roles/{id}")
def eliminar_rol(id: str):
    try:
        # Check if users have this role
        usuarios = supabase.table("usuarios").select("id").eq("rol_id", id).execute()
        if usuarios.data:
            raise HTTPException(status_code=400, detail="No se puede eliminar un rol asociado a usuarios.")
            
        respuesta = supabase.table("roles").delete().eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Rol no encontrado")
        return {"estado": "Exito", "mensaje": "Rol eliminado exitosamente"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Usuarios
@app.get("/api/v1/usuarios")
def obtener_usuarios():
    try:
        # Se une con la tabla roles
        respuesta = supabase.table("usuarios").select("*, roles(nombre)").execute()
        datos_listos = []
        for u in respuesta.data:
            rol_info = u.get("roles")
            datos_listos.append({
                "id": u.get("id"),
                "nombre_completo": u.get("nombre_completo"),
                "email": u.get("email"),
                "rol_id": u.get("rol_id"),
                "nombre_rol": rol_info.get("nombre") if rol_info else "Sin Rol",
                "activo": u.get("activo"),
                "creado_en": u.get("creado_en")
            })
        return {"estado": "Exito", "datos": datos_listos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/usuarios")
def crear_usuario(usuario: UsuarioCreate):
    try:
        datos = usuario.dict()
        plain_pass = datos.pop("password")
        datos["password_hash"] = get_password_hash(plain_pass)
        
        respuesta = supabase.table("usuarios").insert(datos).execute()
        user_data = respuesta.data[0]
        user_data.pop("password_hash", None)
        return {"estado": "Exito", "datos": user_data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando usuario: {str(e)}")

@app.put("/api/v1/usuarios/{id}")
def actualizar_usuario(id: str, usuario: UsuarioUpdate):
    try:
        datos = usuario.dict(exclude_unset=True) # Solo actualiza los enviados
        if "password" in datos and datos["password"]:
            datos["password_hash"] = get_password_hash(datos.pop("password"))
        elif "password" in datos:
            datos.pop("password") # Si mandaron string vacio, no actualizamos
            
        respuesta = supabase.table("usuarios").update(datos).eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        
        user_data = respuesta.data[0]
        user_data.pop("password_hash", None)
        return {"estado": "Exito", "datos": user_data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.patch("/api/v1/usuarios/{id}/estado")
def cambiar_estado_usuario(id: str, activo: bool):
    try:
        respuesta = supabase.table("usuarios").update({"activo": activo}).eq("id", id).execute()
        if not respuesta.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        return {"estado": "Exito", "mensaje": f"Usuario {'activado' if activo else 'desactivado'}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Modulos y Permisos
@app.get("/api/v1/permisos")
def obtener_todos_permisos():
    try:
        respuesta = supabase.table("permisos").select("*").execute()
        return {"estado": "Exito", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/roles/{rol_id}/permisos")
def obtener_permisos_rol(rol_id: str):
    try:
        # Se obtiene la lista de permisos para este rol
        respuesta = supabase.table("rol_permisos").select("permisos_id").eq("rol_id", rol_id).execute()
        permisos_ids = [p["permisos_id"] for p in respuesta.data] if respuesta.data else []
        return {"estado": "Exito", "datos": permisos_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class PermisoActualizacion(BaseModel):
    permisos_ids: List[str]

@app.put("/api/v1/roles/{rol_id}/permisos")
def actualizar_permisos_rol(rol_id: str, datos: PermisoActualizacion):
    try:
        # Primero borrar los existentes para este rol
        supabase.table("rol_permisos").delete().eq("rol_id", rol_id).execute()
        
        # Insertar los nuevos
        if datos.permisos_ids:
            nuevos = [{"rol_id": rol_id, "permisos_id": pid} for pid in datos.permisos_ids]
            supabase.table("rol_permisos").insert(nuevos).execute()
                
        return {"estado": "Exito", "mensaje": "Permisos actualizados correctamente"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Autenticacion
@app.post("/api/v1/auth/login")
def login_usuario(login_data: LoginRequest):
    try:
        # Busca al usuario por email
        respuesta = supabase.table("usuarios").select("*, roles(nombre)").eq("email", login_data.email).execute()
        usuarios = respuesta.data
        
        if not usuarios:
            raise HTTPException(status_code=401, detail="Correo electrónico o contraseña incorrectos")
            
        usuario = usuarios[0]
        
        if not usuario.get("activo"):
            raise HTTPException(status_code=403, detail="Esta cuenta de usuario ha sido desactivada")
            
        # Verificar password
        if not verify_password(login_data.password, usuario.get("password_hash")):
            raise HTTPException(status_code=401, detail="Correo electrónico o contraseña incorrectos")
            
        # Preparar datos a devolver
        rol_info = usuario.get("roles")
        nombre_rol = rol_info.get("nombre") if rol_info else "Sin Rol"
        rol_id = usuario.get("rol_id")
        
        # Obtener los permisos del usuario para inyectarlos en la respuesta del Login
        permisos_respuesta = supabase.table("rol_permisos").select("permisos(codigo)").eq("rol_id", rol_id).execute()
        
        permisos_acciones = []
        if permisos_respuesta.data:
            for p in permisos_respuesta.data:
                per = p.get("permisos")
                if per and isinstance(per, dict) and per.get("codigo"):
                    permisos_acciones.append(per.get("codigo"))
                # If Supabase returns a list instead because of 1:M implicitly:
                elif per and isinstance(per, list) and len(per) > 0 and per[0].get("codigo"):
                    permisos_acciones.append(per[0].get("codigo"))
        
        return {
            "estado": "Exito", 
            "datos": {
                "id": usuario.get("id"),
                "nombre_completo": usuario.get("nombre_completo"),
                "email": usuario.get("email"),
                "rol": nombre_rol,
                "permisos_acciones": permisos_acciones
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")
