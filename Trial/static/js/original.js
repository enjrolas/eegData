//based on the excellent kaleidoscope code by Nekodigi (https://gist.github.com/Nekodigi/b72a3f110ecca00d1c8b343fe58bf4e2)

let capture;
let r; //kaleidoscope radius
let angle;
let imgAngleOff = 0; //offset angle when uv mapping
let angularSpeed = 0;
let w;
let h;
let showStroke = false;
let img;
let divisions = 0.1;
let u, v;
let uInc, vInc;
let scrambled=true;
var puzzle={};
var firstRun=true;
let images=new Array();
var imageIndex=0;
let divisionSlider;
var lastValue=0;
let font

function preload() {
    font = loadFont('fonts/Inconsolata.otf');
}

function setup() {
    createCanvas(1200, 800, WEBGL);
    let uri = new URL(window.location.href);
    let dataset = uri.searchParams.get('dataset');
    let quantity=uri.searchParams.get('number');
    print(dataset);
    if(dataset==null)
	dataset="";
    else
	dataset=dataset+"/";
    print(quantity)
    if(quantity==null)
	quantity=5;
    //   images.push(loadImage("img/dog.jpg"));
    //    images.push(loadImage("img/whiteMountains.jpg"));
    //    images.push(loadImage("img/maggie.jpg"));
    for(var i=0;i<quantity;
    images.push(loadImage("img/dogLineDrawing.jpg"));
    images.push(loadImage("img/dogText.jpg"));
    images.push(loadImage("img/dogTextSpanish.jpg"));
    images.push(loadImage("img/scarlett.jpg"));
    images.push(loadImage("img/scarlettJohanssonLineDrawing.jpg"));
    images.push(loadImage("img/scarlettText.jpg"));
    divisionSlider=createSlider(1, 1000, 500);
    divisionSlider.position(1000, 20);
    randomize();
	
    angle = (2 * PI) / 20;
    w = height;
    h = height;
    r = width / 2;
    noStroke();
    //  stroke(255);
    textureMode(NORMAL);
}
    
function draw() {
    background(0);
    textFont(font);
    if(divisionSlider.value()!=lastValue)
    {
	divisions=1/sqrt(divisionSlider.value());
	randomize();
	lastValue=divisionSlider.value();
    }
    
    if (showStroke) stroke(255);
    else noStroke();
    translate(-width / 2, -height / 2);
    for (var y = 0; y <1; y += divisions) {
        for (var x = 0; x <1; x += divisions) {
            beginShape(TRIANGLE_STRIP);
            texture(images[imageIndex]);
            if (scrambled) {
		var index=[(int)(x/divisions), (int)(y/divisions)];
		var uv=puzzle[index];
		
                vertex(w * x, h * y, uv[0], uv[1]);             
                vertex(w * x, h * (y + divisions), uv[0], uv[1]+divisions);             
                vertex(w * (x+divisions), h * y, uv[0]+divisions, uv[1]);
		vertex(w * (x+divisions), h * (y + divisions), uv[0]+divisions, uv[1]+divisions);                               
		
            } else {
                vertex(w * x, h * y, x, y);
                vertex(w * x, h * (y + divisions), x, y + divisions);
                vertex(w * (x+divisions), h * y, x+divisions, y);
		vertex(w * (x+divisions), h * (y + divisions), x+divisions, y + divisions);             
		
            }
            endShape(CLOSE);
        }
    }
    fill(255);
    text('puzzle pieces', divisionSlider.x, divisionSlider.y+35);
}

function randomize() {
    puzzle={};
    
    for(var x=0;x<1;x+=divisions)
        for(var y=0;y<1;y+=divisions)
    {
	var index=[(int)(x/divisions),(int)(y/divisions)];
	var uv=[int(random(0,sqrt(divisionSlider.value())))*divisions, int(random(0,sqrt(divisionSlider.value())))*divisions];
	puzzle[index]=uv;
	
    }
}

function mouseMoved() {
    angle = (2 * PI) / map(mouseX, 0, width, 1, 40);
    angularSpeed = map(mouseY - height / 2, 0, height / 2, 0, 0.05);
}
function keyPressed() {
    if (key == "s") showStroke = !showStroke;
    if (key == "r") randomize();
    if(key=='S')
        scrambled=!scrambled;
    if(keyCode==UP_ARROW)
    {
	imageIndex++;
	if(imageIndex>=images.length)
	    imageIndex=0;
    }
    if(keyCode==DOWN_ARROW)
    {
	imageIndex--;
	if(imageIndex<0)
	    imageIndex=images.length-1;
    }
    if(keyCode==RIGHT_ARROW)    
    {    
        divisionSlider.value(divisionSlider.value()+1);    
    }
    if(keyCode==LEFT_ARROW)    
    {    
        divisionSlider.value(divisionSlider.value()-1);    
    }
}

function mouseClicked()
{
    imageIndex++;
    if(imageIndex>=images.length)
	imageIndex=0;
}
