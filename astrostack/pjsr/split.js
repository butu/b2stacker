// astrostack – teilt einen Farb-Master (Dualband an Farbkamera) in Einzelkanäle.
// Aufruf: PixInsight.exe -n --automation-mode -r="split.js,in=<rgb.xisf>,out1=<pfad>,ch1=R,out2=<pfad>,ch2=GB" --force-exit
// Kanäle: R, G, B oder Kombinationen (GB = Mittel aus Grün und Blau).

function exprFor( ch )
{
   var idx = { R: 0, G: 1, B: 2 };
   var parts = [];
   for ( var i = 0; i < ch.length; ++i )
   {
      if ( !( ch[ i ] in idx ) )
         throw new Error( "Unbekannter Kanal: " + ch );
      parts.push( "$T[" + idx[ ch[ i ] ] + "]" );
   }
   return parts.length == 1 ? parts[ 0 ] : "(" + parts.join( "+" ) + ")/" + parts.length;
}

function getArg( name )
{
   for ( var i = 0; i < jsArguments.length; ++i )
   {
      var k = jsArguments[ i ].indexOf( "=" );
      if ( k > 0 && jsArguments[ i ].substring( 0, k ) == name )
         return jsArguments[ i ].substring( k + 1 );
   }
   return null;
}

function main()
{
   var input = getArg( "in" );
   if ( !input )
      throw new Error( "Argument in= fehlt" );
   var ws = ImageWindow.open( input );
   if ( ws == null || ws.length < 1 )
      throw new Error( "Kann nicht öffnen: " + input );
   var w = ws[ 0 ];
   for ( var n = 1; n <= 4; ++n )
   {
      var out = getArg( "out" + n ), ch = getArg( "ch" + n );
      if ( !out || !ch )
         break;
      var expr = exprFor( ch.toUpperCase() );
      var id = "astrostack_split_" + n;
      var P = new PixelMath;
      P.expression = expr; P.useSingleExpression = true; P.createNewImage = true; P.showNewImage = true;
      P.newImageId = id; P.newImageColorSpace = PixelMath.prototype.Gray; P.newImageSampleFormat = PixelMath.prototype.f32;
      P.rescale = false; P.truncate = false;
      P.executeOn( w.mainView );
      var wo = ImageWindow.windowById( id );
      if ( wo == null || wo.isNull )
         throw new Error( "PixelMath fehlgeschlagen: " + expr );
      wo.saveAs( out, false, false, false, false );
      wo.forceClose();
      console.writeln( "SPLIT WROTE " + out );
   }
   w.forceClose();
}

try
{
   main();
   console.writeln( "SPLIT OK" );
}
catch ( e )
{
   console.criticalln( "SPLIT FAILED: " + e.message );
}
